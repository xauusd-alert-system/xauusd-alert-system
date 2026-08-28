# -*- coding: utf-8 -*-
"""Session quality score for signal ranking.

Combines three dimensions into a single 0-100 quality score:
1. Volume quality (0-40): breakout volume relative to average
2. Time-of-day quality (0-30): prime window vs degraded
3. Regime quality (0-30): trending vs ranging market

Used to rank signals and optionally filter low-quality setups.
"""

from __future__ import annotations

import datetime as dt

# --- Time-of-day scoring ---

# Session boundaries (UTC)
SESSION_START_SEC = 13 * 3600 + 30 * 60  # 13:30 = 18:30 local
SESSION_END_SEC = 19 * 3600 + 55 * 60  # 19:55 = 00:55 local

# Prime window: first 30-90 min after open (strongest moves per ORB research)
PRIME_START_SEC = SESSION_START_SEC + 30 * 60  # 14:00 = 19:00 local
PRIME_END_SEC = SESSION_START_SEC + 90 * 60  # 15:00 = 20:00 local

# Good window: 90-180 min (mid-morning, still decent activity)
GOOD_END_SEC = SESSION_START_SEC + 180 * 60  # 16:30 = 21:30 local

# Degraded: last 45 min (position squaring, weak trends)
DEGRADED_START_SEC = SESSION_END_SEC - 45 * 60  # 19:10 = 00:10 local


def _time_of_day_score(signal_ts: int) -> float:
    """Score 0-30 based on when the signal fires.

    Prime (30-90 min): 30 pts — strongest trends, highest volume
    Good (90-180 min): 20 pts — still decent
    Early (0-30 min): 15 pts — observation period, no entries yet usually
    Late (>180 min): 10 pts — fatigue setting in
    Degraded (last 45 min): 0 pts — avoid
    """
    utc = dt.datetime.fromtimestamp(signal_ts, dt.UTC)
    sec = utc.hour * 3600 + utc.minute * 60 + utc.second

    if sec < SESSION_START_SEC or sec > SESSION_END_SEC:
        return 0  # outside session
    if sec >= DEGRADED_START_SEC:
        return 0  # degraded window
    if PRIME_START_SEC <= sec <= PRIME_END_SEC:
        return 30  # prime
    if PRIME_END_SEC < sec <= GOOD_END_SEC:
        return 20  # good
    if SESSION_START_SEC <= sec < PRIME_START_SEC:
        return 15  # early (observation)
    return 10  # late afternoon


# --- Volume scoring ---


def _volume_score(volume_ratio: float) -> float:
    """Score 0-40 based on breakout volume vs average.

    volume_ratio = breakout bar volume / avg volume (20-bar lookback)
    >2.0x: 40 pts (exceptional)
    1.5-2.0x: 35 pts (strong)
    1.2-1.5x: 25 pts (above average)
    1.0-1.2x: 15 pts (average)
    0.8-1.0x: 5 pts (below average)
    <0.8x: 0 pts (weak)
    """
    if volume_ratio >= 2.0:
        return 40
    if volume_ratio >= 1.5:
        return 35
    if volume_ratio >= 1.2:
        return 25
    if volume_ratio >= 1.0:
        return 15
    if volume_ratio >= 0.8:
        return 5
    return 0


# --- Regime scoring ---


def _regime_score(regime: str, bias: str) -> float:
    """Score 0-30 based on market regime alignment.

    Trend aligned (trend_up + long, trend_down + short): 30 pts
    Trend opposed (trend_up + short, trend_down + long): 5 pts
    Range: 15 pts (neutral)
    Compression: 10 pts (low vol, unreliable)
    Unknown: 12 pts (default)
    """
    regime = regime.strip().lower() if regime else "unknown"
    bias = bias.strip().lower() if bias else ""

    if regime in ("trend_up", "trend_down"):
        # Check alignment
        if (regime == "trend_up" and bias == "long") or (regime == "trend_down" and bias == "short"):
            return 30  # aligned
        elif bias in ("long", "short"):
            return 5  # opposed
        return 15  # trend but no bias? shouldn't happen
    elif regime == "range":
        return 15
    elif regime == "compression":
        return 10
    else:
        return 12  # unknown/flat


# --- Composite score ---


def compute_quality_score(
    signal_ts: int,
    volume_ratio: float = 1.0,
    regime: str = "",
    bias: str = "",
) -> dict:
    """Compute composite session quality score (0-100).

    Args:
        signal_ts: UNIX timestamp of the signal
        volume_ratio: breakout volume / average volume
        regime: market regime (trend_up, trend_down, range, compression)
        bias: signal direction (long, short)

    Returns dict with:
        - total: 0-100 composite score
        - volume: 0-40 component
        - time_of_day: 0-30 component
        - regime: 0-30 component
        - grade: A/B/C/D quality grade
        - reasoning: human-readable explanation
    """
    vol = _volume_score(volume_ratio)
    tod = _time_of_day_score(signal_ts)
    reg = _regime_score(regime, bias)
    total = vol + tod + reg

    # Grade
    if total >= 80:
        grade = "A"
    elif total >= 60:
        grade = "B"
    elif total >= 40:
        grade = "C"
    else:
        grade = "D"

    # Reasoning
    reasons = []
    if vol >= 30:
        reasons.append(f"strong volume ({volume_ratio:.1f}x avg)")
    elif vol >= 20:
        reasons.append(f"above-avg volume ({volume_ratio:.1f}x)")
    elif vol <= 5:
        reasons.append(f"weak volume ({volume_ratio:.1f}x)")

    if tod >= 25:
        reasons.append("prime time window")
    elif tod >= 15:
        reasons.append("good time window")
    elif tod <= 5:
        reasons.append("degraded time window")

    if reg >= 25:
        reasons.append(f"regime aligned ({regime}+{bias})")
    elif reg <= 10:
        reasons.append(f"regime unfavorable ({regime})")

    return {
        "total": total,
        "volume": vol,
        "time_of_day": tod,
        "regime": reg,
        "grade": grade,
        "reasoning": "; ".join(reasons) if reasons else "no data",
    }


def format_quality(score: dict) -> str:
    """Format quality score for display."""
    return (
        f"Quality {score['total']}/100 [{score['grade']}] — "
        f"vol={score['volume']}/40  tod={score['time_of_day']}/30  "
        f"reg={score['regime']}/30 — {score['reasoning']}"
    )
