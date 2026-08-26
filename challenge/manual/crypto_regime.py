# -*- coding: utf-8 -*-
"""Crypto sell-off detector.

Uses BTC price action as a proxy for crypto market sentiment.
When BTC is in a significant drawdown, crypto_beta stocks tend to
sell off together, making long setups unreliable and short setups
more likely to hit stops on reversals.

Detection methods:
1. Intraday drawdown: current price vs session high
2. Multi-day drawdown: current price vs N-day high
3. Momentum: short-term EMA slope

All signals are combined into a single regime score:
- 0-20: SEVERE sell-off (block all trades)
- 21-40: MILD sell-off (block longs, allow shorts)
- 41-60: NEUTRAL (normal trading)
- 61-80: MILD rally (normal trading)
- 81-100: STRONG rally (prefer longs)
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_FILE = os.path.join(ROOT, "data", "manual", "crypto_regime_cache.json")


@dataclass
class CryptoRegime:
    """Current crypto market regime assessment."""
    score: int  # 0-100 (0=severe sell-off, 100=strong rally)
    label: str  # SEVERE_SELL | MILD_SELL | NEUTRAL | MILD_RALLY | STRONG_RALLY
    btc_intraday_dd_pct: float  # drawdown from session high
    btc_multiday_dd_pct: float  # drawdown from N-day high
    btc_ema_slope_pct: float  # short-term EMA slope
    block_longs: bool
    block_shorts: bool
    reason: str


def _ema(values: list, period: int) -> float:
    """Exponential moving average."""
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


def classify_crypto_regime(
    btc_candles: list,
    lookback_days: int = 5,
    session_start_utc: dt.time = None,
) -> CryptoRegime:
    """Classify crypto market regime from BTC candle data.
    
    Args:
        btc_candles: list of 1-min candles with {time, open, high, low, close, volume}
        lookback_days: days to look back for multi-day high
        session_start_utc: session start time (default 13:30)
    
    Returns:
        CryptoRegime with score, label, and trading recommendations
    """
    if session_start_utc is None:
        session_start_utc = dt.time(13, 30)
    
    if not btc_candles:
        return CryptoRegime(
            score=50, label="NEUTRAL",
            btc_intraday_dd_pct=0.0, btc_multiday_dd_pct=0.0,
            btc_ema_slope_pct=0.0,
            block_longs=False, block_shorts=False,
            reason="no BTC data available"
        )
    
    # Sort by time
    candles = sorted(btc_candles, key=lambda x: x["time"])
    now_ts = candles[-1]["time"]
    now = dt.datetime.fromtimestamp(now_ts, dt.timezone.utc)
    
    # --- Intraday drawdown ---
    # Find session high (from today's session start)
    today = now.date()
    sess_start_ts = int(dt.datetime.combine(today, session_start_utc, tzinfo=dt.timezone.utc).timestamp())
    session_candles = [c for c in candles if c["time"] >= sess_start_ts]
    
    if session_candles:
        session_high = max(c["high"] for c in session_candles)
        current_price = session_candles[-1]["close"]
        intraday_dd = (session_high - current_price) / session_high * 100 if session_high > 0 else 0
    else:
        intraday_dd = 0.0
        current_price = candles[-1]["close"]
    
    # --- Multi-day drawdown ---
    # Find N-day high
    lookback_ts = now_ts - lookback_days * 86400
    lookback_candles = [c for c in candles if c["time"] >= lookback_ts]
    
    if lookback_candles:
        multiday_high = max(c["high"] for c in lookback_candles)
        multiday_dd = (multiday_high - current_price) / multiday_high * 100 if multiday_high > 0 else 0
    else:
        multiday_dd = 0.0
    
    # --- EMA slope (short-term momentum) ---
    closes = [c["close"] for c in candles[-60:]]  # last 60 minutes
    if len(closes) >= 20:
        ema_now = _ema(closes, 10)
        ema_prev = _ema(closes[:-10], 10) if len(closes) > 10 else closes[0]
        ema_slope = (ema_now - ema_prev) / ema_prev * 100 if ema_prev > 0 else 0
    else:
        ema_slope = 0.0
    
    # --- Combine into score ---
    # Intraday drawdown: 0% = 50 pts, 2%+ = 0 pts
    intraday_score = max(0, min(50, 50 - intraday_dd * 25))
    
    # Multi-day drawdown: 0% = 30 pts, 5%+ = 0 pts
    multiday_score = max(0, min(30, 30 - multiday_dd * 6))
    
    # EMA slope: -0.1% = 0 pts, 0% = 20 pts, +0.1% = 20 pts (capped)
    ema_score = max(0, min(20, 20 + ema_slope * 100))
    
    total_score = int(intraday_score + multiday_score + ema_score)
    total_score = max(0, min(100, total_score))
    
    # --- Classify ---
    if total_score <= 20:
        label = "SEVERE_SELL"
        block_longs = True
        block_shorts = True  # even shorts risky on reversals
        reason = f"BTC sell-off: intraday -{intraday_dd:.1f}%, multiday -{multiday_dd:.1f}%"
    elif total_score <= 40:
        label = "MILD_SELL"
        block_longs = True
        block_shorts = False
        reason = f"BTC weakness: intraday -{intraday_dd:.1f}%, multiday -{multiday_dd:.1f}%"
    elif total_score <= 60:
        label = "NEUTRAL"
        block_longs = False
        block_shorts = False
        reason = "Crypto market neutral"
    elif total_score <= 80:
        label = "MILD_RALLY"
        block_longs = False
        block_shorts = False
        reason = f"BTC mild rally: +{ema_slope:.2f}%"
    else:
        label = "STRONG_RALLY"
        block_longs = False
        block_shorts = False
        reason = f"BTC strong rally: +{ema_slope:.2f}%, intraday +{intraday_dd:.1f}%"
    
    return CryptoRegime(
        score=total_score,
        label=label,
        btc_intraday_dd_pct=round(intraday_dd, 2),
        btc_multiday_dd_pct=round(multiday_dd, 2),
        btc_ema_slope_pct=round(ema_slope, 4),
        block_longs=block_longs,
        block_shorts=block_shorts,
        reason=reason,
    )


def should_block_trade(
    regime: CryptoRegime,
    bias: str,
    cluster: str = "",
) -> tuple[bool, str]:
    """Check if a trade should be blocked based on crypto regime.
    
    Args:
        regime: current CryptoRegime
        bias: trade direction (long/short)
        cluster: asset cluster name (e.g. "crypto_beta")
    
    Returns:
        (blocked: bool, reason: str)
    """
    # Only apply to crypto_beta cluster
    if cluster != "crypto_beta":
        return False, ""
    
    if bias == "long" and regime.block_longs:
        return True, f"crypto sell-off ({regime.label}): longs blocked"
    if bias == "short" and regime.block_shorts:
        return True, f"crypto sell-off ({regime.label}): shorts blocked"
    
    return False, ""


def save_regime(regime: CryptoRegime) -> None:
    """Save regime to cache for monitoring."""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    data = {
        "score": regime.score,
        "label": regime.label,
        "btc_intraday_dd_pct": regime.btc_intraday_dd_pct,
        "btc_multiday_dd_pct": regime.btc_multiday_dd_pct,
        "btc_ema_slope_pct": regime.btc_ema_slope_pct,
        "reason": regime.reason,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_regime() -> Optional[CryptoRegime]:
    """Load last cached regime."""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return CryptoRegime(
            score=data["score"],
            label=data["label"],
            btc_intraday_dd_pct=data.get("btc_intraday_dd_pct", 0),
            btc_multiday_dd_pct=data.get("btc_multiday_dd_pct", 0),
            btc_ema_slope_pct=data.get("btc_ema_slope_pct", 0),
            block_longs=data["score"] <= 40,
            block_shorts=data["score"] <= 20,
            reason=data.get("reason", ""),
        )
    except Exception:
        return None


def format_regime(regime: CryptoRegime) -> str:
    """Format regime for Telegram display."""
    emoji = {
        "SEVERE_SELL": "🔴",
        "MILD_SELL": "🟠",
        "NEUTRAL": "⚪",
        "MILD_RALLY": "🟢",
        "STRONG_RALLY": "🟢",
    }.get(regime.label, "⚪")
    
    return (
        f"{emoji} Crypto Regime: {regime.label} ({regime.score}/100)\n"
        f"BTC intraday: -{regime.btc_intraday_dd_pct:.1f}% | "
        f"multiday: -{regime.btc_multiday_dd_pct:.1f}% | "
        f"EMA: {regime.btc_ema_slope_pct:+.2f}%\n"
        f"Longs: {'BLOCKED' if regime.block_longs else 'ok'} | "
        f"Shorts: {'BLOCKED' if regime.block_shorts else 'ok'}"
    )
