# -*- coding: utf-8 -*-
"""Live-calibrated quality score.

Extends quality_score.py by adjusting component weights based on actual
trade outcomes. The base quality_score uses fixed weights (vol 40, tod 30,
regime 30). This module learns from live data which components actually
predict profitable trades.

Usage:
    from challenge.manual.quality_score_live import compute_live_quality
    score = compute_live_quality(signal_ts, volume_ratio, regime, bias)
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CALIBRATION_FILE = os.path.join(ROOT, "data", "manual", "quality_calibration_live.json")
MANUAL_CFG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "manual_config.yaml")

# Default weights (same as base quality_score)
DEFAULT_WEIGHTS = {
    "volume": 40,
    "time_of_day": 30,
    "regime": 30,
}

# Minimum trades per component value to recalibrate.
# Overridable via manual_config.yaml key: min_trades_per_bucket
DEFAULT_MIN_TRADES_PER_BUCKET = 5


def _load_min_trades_per_bucket() -> int:
    """Read min_trades_per_bucket from manual_config.yaml (fallback default)."""
    try:
        if os.path.isfile(MANUAL_CFG_PATH):
            with open(MANUAL_CFG_PATH, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            value = int(cfg.get("min_trades_per_bucket", DEFAULT_MIN_TRADES_PER_BUCKET))
            return value if value > 0 else DEFAULT_MIN_TRADES_PER_BUCKET
    except Exception:  # pragma: no cover - config-dependent; never fatal
        pass
    return DEFAULT_MIN_TRADES_PER_BUCKET


MIN_TRADES_PER_BUCKET = _load_min_trades_per_bucket()


def _load_calibration() -> dict:
    """Load live calibration data."""
    if not os.path.exists(CALIBRATION_FILE):
        return {"weights": dict(DEFAULT_WEIGHTS), "bucket_stats": {}}
    try:
        with open(CALIBRATION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"weights": dict(DEFAULT_WEIGHTS), "bucket_stats": {}}


def _save_calibration(data: dict) -> None:
    """Save calibration data."""
    os.makedirs(os.path.dirname(CALIBRATION_FILE), exist_ok=True)
    with open(CALIBRATION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _bucket_key(component: str, value: float) -> str:
    """Map continuous component value to a discrete bucket."""
    if component == "volume":
        if value >= 2.0:
            return "vol_2.0+"
        elif value >= 1.5:
            return "vol_1.5-2.0"
        elif value >= 1.2:
            return "vol_1.2-1.5"
        elif value >= 1.0:
            return "vol_1.0-1.2"
        elif value >= 0.8:
            return "vol_0.8-1.0"
        else:
            return "vol_<0.8"
    elif component == "time_of_day":
        if value >= 25:
            return "tod_prime"
        elif value >= 15:
            return "tod_good"
        elif value >= 5:
            return "tod_early"
        else:
            return "tod_late"
    elif component == "regime":
        if value >= 25:
            return "reg_aligned"
        elif value >= 15:
            return "reg_range"
        else:
            return "reg_unfav"
    return "unknown"


def record_outcome(
    volume_ratio: float,
    tod_score: float,
    regime_score: float,
    r_outcome: float,
) -> None:
    """Record a trade outcome for live calibration.
    
    Args:
        volume_ratio: breakout volume / avg volume
        tod_score: time-of-day component score (0-30)
        regime_score: regime component score (0-30)
        r_outcome: trade outcome in R units (+3.5 for win, -1.0 for loss)
    """
    cal = _load_calibration()
    buckets = cal.setdefault("bucket_stats", {})
    
    # Record for each component
    for component, value in [("volume", volume_ratio), 
                              ("time_of_day", tod_score),
                              ("regime", regime_score)]:
        key = _bucket_key(component, value)
        bucket = buckets.setdefault(key, {"trades": 0, "sum_r": 0.0, "wins": 0})
        bucket["trades"] += 1
        bucket["sum_r"] = round(bucket["sum_r"] + r_outcome, 3)
        if r_outcome > 0:
            bucket["wins"] += 1
    
    _save_calibration(cal)


def recalibrate_weights() -> dict:
    """Recalibrate component weights based on live outcomes.
    
    Components that better predict wins get higher weights.
    Uses avgR per bucket as the signal quality metric.
    
    Returns new weight dict.
    """
    cal = _load_calibration()
    buckets = cal.get("bucket_stats", {})
    
    # Compute avgR per component
    component_avg_r = {}
    for component in ["volume", "time_of_day", "regime"]:
        relevant_buckets = {k: v for k, v in buckets.items() 
                           if k.startswith(component[:3])}
        total_trades = sum(b["trades"] for b in relevant_buckets.values())
        total_r = sum(b["sum_r"] for b in relevant_buckets.values())
        
        if total_trades >= MIN_TRADES_PER_BUCKET:
            component_avg_r[component] = total_r / total_trades
        else:
            component_avg_r[component] = 0.0
    
    # Convert avgR to weights (higher avgR = higher weight)
    # Normalize so weights sum to 100
    total_avg_r = sum(max(0, v) for v in component_avg_r.values())
    if total_avg_r <= 0:
        return dict(DEFAULT_WEIGHTS)
    
    new_weights = {}
    for comp, avg_r in component_avg_r.items():
        # Minimum weight of 10 to avoid zeroing out any component
        new_weights[comp] = max(10, int(100 * max(0, avg_r) / total_avg_r))
    
    # Normalize to sum to 100
    total = sum(new_weights.values())
    if total > 0:
        for comp in new_weights:
            new_weights[comp] = round(new_weights[comp] * 100 / total)
    
    cal["weights"] = new_weights
    _save_calibration(cal)
    return new_weights


def get_live_weights() -> dict:
    """Get current calibrated weights."""
    cal = _load_calibration()
    return cal.get("weights", dict(DEFAULT_WEIGHTS))


def compute_live_quality(
    signal_ts: int,
    volume_ratio: float = 1.0,
    regime: str = "",
    bias: str = "",
    tod_score: float = 0.0,
    regime_score: float = 0.0,
) -> dict:
    """Compute quality score using live-calibrated weights.
    
    Args:
        signal_ts: UNIX timestamp of the signal
        volume_ratio: breakout volume / avg volume
        regime: market regime (trend_up, trend_down, range, compression)
        bias: signal direction (long, short)
        tod_score: pre-computed time-of-day score (0-30)
        regime_score: pre-computed regime score (0-30)
    
    Returns dict with total score, components, and grade.
    """
    from challenge.manual.quality_score import (
        _volume_score, _time_of_day_score, _regime_score
    )
    
    weights = get_live_weights()
    
    # Compute raw component scores
    vol_raw = _volume_score(volume_ratio)
    tod_raw = tod_score if tod_score > 0 else _time_of_day_score(signal_ts)
    reg_raw = regime_score if regime_score > 0 else _regime_score(regime, bias)
    
    # Apply calibrated weights (scale 0-100 component to 0-weight range)
    vol_calibrated = vol_raw * weights.get("volume", 40) / 40
    tod_calibrated = tod_raw * weights.get("time_of_day", 30) / 30
    reg_calibrated = reg_raw * weights.get("regime", 30) / 30
    
    total = int(vol_calibrated + tod_calibrated + reg_calibrated)
    total = max(0, min(100, total))
    
    # Grade
    if total >= 80:
        grade = "A"
    elif total >= 60:
        grade = "B"
    elif total >= 40:
        grade = "C"
    else:
        grade = "D"
    
    return {
        "total": total,
        "volume": int(vol_calibrated),
        "time_of_day": int(tod_calibrated),
        "regime": int(reg_calibrated),
        "grade": grade,
        "weights": weights,
        "raw": {"volume": vol_raw, "time_of_day": tod_raw, "regime": reg_raw},
    }
