# -*- coding: utf-8 -*-
"""Quality score integration for scanner — reusable function shared by all 3 setup types."""
from __future__ import annotations
import datetime as dt
from challenge.manual.quality_score import compute_quality_score


def compute_quality_for_setup(
    candles_1m: list,
    date,
    signal_bar: dict | None,
    impulse_bar: dict | None,
    trend15: str = "",
    bias: str = "",
    setup_type: str = "",
    entry: float = 0.0,
    target: float = 0.0,
) -> int:
    """Compute 0-100 quality score for a setup.
    
    Uses the session quality scorer with setup-specific adjustments:
    - gap_fade: bigger gap = confidence bonus
    - opening_drive: stronger body = confidence bonus  
    - impulse: uses the default scorer as-is
    
    Returns total score (0-100).
    """
    # Determine signal timestamp
    signal_ts = None
    if signal_bar:
        signal_ts = signal_bar["time"]
    elif impulse_bar:
        signal_ts = impulse_bar["time"]
    else:
        # Fallback: session open + 1 minute
        from .scanner import SESSION_START_UTC
        sess_start = dt.datetime.combine(
            date, SESSION_START_UTC, tzinfo=dt.timezone.utc
        ).timestamp()
        signal_ts = int(sess_start + 60)

    # Compute volume ratio for signal bar
    vol_ratio = 1.0
    if signal_bar and signal_bar.get("volume", 0) > 0:
        from .scanner import bars_of_day
        day = bars_of_day(candles_1m, date)
        vols = [c.get("volume", 0) for c in day if c.get("volume", 0) > 0]
        if vols:
            avg_vol = sum(vols[-20:]) / min(20, len(vols))
            if avg_vol > 0:
                vol_ratio = signal_bar["volume"] / avg_vol

    # Compute quality
    score = compute_quality_score(
        signal_ts=int(signal_ts),
        volume_ratio=vol_ratio,
        regime=trend15,
        bias=bias,
    )

    # Setup-specific adjustments (from calibration on 386 setups, 2026-08-24)
    if setup_type == "gap_fade":
        # Bigger gap = higher conviction fade
        if entry > 0 and target > 0:
            gap_size = abs(entry - target) / target
            if gap_size > 0.02:
                score["total"] = min(100, score["total"] + 10)
            elif gap_size < 0.008:
                score["total"] = max(0, score["total"] - 10)
    elif setup_type == "opening_drive":
        # Stronger drive body = higher conviction
        if signal_bar:
            body = abs(signal_bar["close"] - signal_bar["open"])
            rng = signal_bar["high"] - signal_bar["low"]
            if rng > 0 and body / rng > 0.7:
                score["total"] = min(100, score["total"] + 10)
    # impulse: no adjustment needed, the default scorer already handles regime/time/volume

    # Store component breakdown for live calibration
    score["volume_ratio"] = vol_ratio
    return score


# Quality thresholds per setup type (calibrated on 386 setups, 2026-08-24)
# Thresholds chosen to maximize avgR while keeping >= 14 trades per type.
# Can be overridden by challenge/manual/quality_thresholds.json
_DEFAULT_THRESHOLDS = {
    "impulse": 70,        # avgR +0.264 (up from +0.086), 14 trades
    "gap_fade": 45,       # avgR +1.476 (up from +0.402), 24 trades  
    "opening_drive": 60,  # avgR +0.800 (up from +0.213), 35 trades
}

import json, os
_THRESHOLDS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "quality_thresholds.json")

def _load_thresholds() -> dict:
    """Load thresholds from JSON file, falling back to hardcoded defaults."""
    try:
        if os.path.exists(_THRESHOLDS_FILE):
            with open(_THRESHOLDS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            # Extract only numeric threshold keys
            thresholds = {}
            for stype in ("impulse", "gap_fade", "opening_drive"):
                if stype in data:
                    thresholds[stype] = int(data[stype])
            if thresholds:
                return thresholds
    except Exception:
        pass
    return dict(_DEFAULT_THRESHOLDS)

QUALITY_THRESHOLDS = _load_thresholds()


def passes_quality_filter(setup_type: str, quality_score: int) -> bool:
    """Check if a setup passes its type-specific quality threshold."""
    threshold = QUALITY_THRESHOLDS.get(setup_type, 0)
    return quality_score >= threshold