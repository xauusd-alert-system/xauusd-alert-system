# -*- coding: utf-8 -*-
"""Deterministic synthetic sessions for VWAP Pullback tests (ТЗ §12).

`build_session` scripts the day at 5m granularity and expands each 5m bar
into five 1m bars tracing its path, so rules can be tuned in one place.
All timestamps are America/New_York aware.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from usstocks.models import Bar

NY = ZoneInfo("America/New_York")


def _day(day: str) -> datetime:
    d = datetime.fromisoformat(day)
    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)


def _expand(bar5: Tuple[float, float, float, float, float],
            start: datetime, vol: float) -> List[Bar]:
    """Expand one scripted 5m bar (o,h,l,c) into five 1m bars."""
    o, h, l, c = bar5
    path = [o + (c - o) * k / 4 for k in range(5)]
    wick_up, wick_dn = h - max(o, c), min(o, c) - l
    bars: List[Bar] = []
    for k in range(5):
        op = path[k]
        cl = path[min(k + 1, 4)] if o == c else path[min(k + 1, 4)]
        hi = max(op, cl) + (wick_up / 2 if k in (1, 3) else 0.0)
        lo = min(op, cl) - (wick_dn / 2 if k in (1, 3) else 0.0)
        bars.append(Bar(ts=start + timedelta(minutes=k), open=op, high=hi,
                        low=lo, close=cl, volume=vol / 5))
    return bars


def build_session(script: List[Tuple[Tuple[float, float, float, float], float]],
                  symbol: str = "AMD", day: str = "2026-08-26") -> List[Bar]:
    """script: list of ((o,h,l,c), volume) at 09:35 onward, 5m steps.

    The pre-session context is empty; OR15 covers the first three entries.
    """
    out: List[Bar] = []
    t = _day(day)
    for (ohlc, vol) in script:
        out.extend(_expand(ohlc, t, vol))
        t += timedelta(minutes=5)
    return out


# --------------------------------------------------------------------------
# Scenario builders
# --------------------------------------------------------------------------

BASE_VOL = 100_000.0

LONG_SCRIPT = [
    ((100.00, 100.10, 99.95, 100.05), BASE_VOL),          # OR bar 1 (flat)
    ((100.05, 100.15, 100.00, 100.10), BASE_VOL),         # OR bar 2
    ((100.10, 100.20, 100.05, 100.15), BASE_VOL),         # OR bar 3
    ((100.15, 100.30, 100.10, 100.25), BASE_VOL * 0.8),   # quiet lift
    ((100.25, 101.00, 100.22, 100.90), BASE_VOL * 2.2),   # impulse 1
    ((100.90, 101.45, 100.85, 101.35), BASE_VOL * 2.6),   # impulse 2 (+~1.2%)
    ((101.35, 101.40, 101.05, 101.10), BASE_VOL * 0.7),   # pullback
    ((101.10, 101.15, 100.80, 100.85), BASE_VOL * 0.55),  # pullback -> vwap
    ((100.85, 100.90, 100.62, 100.68), BASE_VOL * 0.45),  # touch/probe vwap
    ((100.68, 100.85, 100.60, 100.78), BASE_VOL * 0.50),  # confirm close >vwap
]


def long_scenario() -> List[Bar]:
    return build_session(LONG_SCRIPT)


def short_scenario() -> List[Bar]:
    inv = [((200 - o, 200 - l, 200 - h, 200 - c), v) for (o, h, l, c), v in LONG_SCRIPT]
    return build_session(inv)


def flat_scenario() -> List[Bar]:
    s = [((100.00, 100.06, 99.96, 100.02), BASE_VOL)] * 10
    return build_session(s)


def to_csv_rows(bars: List[Bar], symbol: str) -> List[str]:
    rows = ["symbol,ts,open,high,low,close,volume"]
    for b in bars:
        rows.append(f"{symbol},{b.ts.isoformat()},{b.open:.4f},{b.high:.4f},"
                    f"{b.low:.4f},{b.close:.4f},{b.volume:.0f}")
    return rows


def _benchmark_1m(steps: int, drift_early: float, drift_late: float,
                  day: str, start: float) -> List[Bar]:
    """Benchmark as 1M bars (provider resolution): gentle early drift, then a
    steady trend, so cumulative VWAP stays on the trailing side of price."""
    out: List[Bar] = []
    price = start
    t = _day(day)
    total = steps * 5
    for i in range(total):
        drift = drift_late if i >= 20 else drift_early
        o = price
        c = price + drift / 5
        out.append(Bar(ts=t + timedelta(minutes=i), open=o, high=c + 0.05,
                       low=o - 0.05, close=c, volume=80_000))
        price = c
    return out


def benchmark_uptrend(bars_n: int = 10, day: str = "2026-08-26") -> List[Bar]:
    return _benchmark_1m(bars_n, 0.15, 0.60, day, 500.0)


def benchmark_downtrend(bars_n: int = 10, day: str = "2026-08-26") -> List[Bar]:
    up = _benchmark_1m(bars_n, 0.15, 0.60, day, 500.0)
    return [Bar(ts=b.ts, open=b.open, high=b.high, low=b.low,
                close=round(1000 - b.close, 4), volume=b.volume) for b in up]
