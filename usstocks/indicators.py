"""Indicators for the VWAP Pullback strategy (ТЗ §7.2).

Rules enforced here:
- only CLOSED bars are consumed (an unclosed 1m bucket is dropped before
  aggregation, an unclosed 5m bar is never returned);
- VWAP restarts at each regular session open (09:30 America/New_York);
- Opening Range = first `range_minutes` worth of CLOSED 5m bars.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from usstocks.models import Bar

NY = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
DEFAULT_RANGE_MINUTES = 15


def ensure_ny(ts: datetime) -> datetime:
    """Attach/convert to NY timezone (aware datetime guaranteed)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=NY)
    return ts.astimezone(NY)


def session_open_dt(day_ts: datetime) -> datetime:
    ts = ensure_ny(day_ts)
    return ts.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute,
                      second=0, microsecond=0)


def drop_unclosed_1m(bars: List[Bar], asof: datetime) -> List[Bar]:
    """Keep 1m bars whose minute bucket is fully closed at `asof`."""
    cutoff = ensure_ny(asof).replace(second=0, microsecond=0)
    return [b for b in bars if ensure_ny(b.ts) < cutoff]


def aggregate_to_5m(bars_1m: List[Bar]) -> List[Bar]:
    """Aggregate sorted 1m bars into 5m buckets keyed on NY wall-clock.

    Buckets are emitted only when all five 1m slots of the bucket are present,
    so a partial live bucket can never be mistaken for a closed bar. Bars from
    different sessions never merge because the bucket key includes the date.
    """
    out: List[Bar] = []
    cur_key: Optional[Tuple] = None
    o = h = l = c = v = None
    count = 0
    for b in sorted(bars_1m, key=lambda x: x.ts):
        nb = ensure_ny(b.ts)
        key = (nb.date(), nb.hour, nb.minute // 5 * 5)
        if key != cur_key:
            if cur_key is not None and count == 5 and o is not None:
                out.append(Bar(ts=_bucket_ts(cur_key), open=o, high=h,
                               low=l, close=c, volume=v))
            cur_key, o, h, l, c, v, count = key, b.open, b.high, b.low, b.close, 0.0, 0
            v = 0.0
        o = b.open if count == 0 else o
        h = max(h, b.high)
        l = min(l, b.low)
        c = b.close
        v += b.volume
        count += 1
    if cur_key is not None and count == 5 and o is not None:
        out.append(Bar(ts=_bucket_ts(cur_key), open=o, high=h, low=l, close=c, volume=v))
    return out


def _bucket_ts(key: Tuple) -> datetime:
    d, hh, mm = key
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY)


def session_vwap_series(bars_5m: List[Bar]) -> List[float]:
    """Cumulative session VWAP per closed 5m bar; resets at 09:30 NY."""
    result: List[float] = []
    cum_pv = cum_v = 0.0
    cur_day = None
    for b in bars_5m:
        nb = ensure_ny(b.ts)
        if cur_day != nb.date():
            cur_day = nb.date()
            cum_pv = cum_v = 0.0
        cum_pv += b.typical_price * max(b.volume, 0.0)
        cum_v += max(b.volume, 0.0)
        result.append(cum_pv / cum_v if cum_v > 0 else b.close)
    return result


def opening_range(bars_5m: List[Bar],
                  range_minutes: int = DEFAULT_RANGE_MINUTES) -> Optional[Tuple[float, float]]:
    """(or_high, or_low) from the first N fully-closed 5m bars of the session.

    Returns None when the range window is not yet complete (ТЗ §7.2: never use
    unclosed bars)."""
    if not bars_5m:
        return None
    n = max(1, range_minutes // 5)
    day = ensure_ny(bars_5m[0].ts).date()
    window = [b for b in bars_5m if ensure_ny(b.ts).date() == day][:n]
    if len(window) < n:
        return None
    return max(b.high for b in window), min(b.low for b in window)


def opening_range_mid(bars_5m: List[Bar],
                      range_minutes: int = DEFAULT_RANGE_MINUTES) -> Optional[float]:
    rng = opening_range(bars_5m, range_minutes)
    return None if rng is None else (rng[0] + rng[1]) / 2.0


def average_volume(bars: List[Bar], upto: int, lookback: int = 20) -> float:
    """Mean volume of the `lookback` bars preceding index `upto` (exclusive)."""
    lo = max(0, upto - lookback)
    window = bars[lo:upto]
    vols = [b.volume for b in window]
    return sum(vols) / len(vols) if vols else 0.0


def volume_ratio(bar: Bar, avg_volume: float) -> float:
    if avg_volume <= 0:
        return 0.0
    return bar.volume / avg_volume


def minutes_until(close_at: datetime, now: datetime) -> float:
    return (ensure_ny(close_at) - ensure_ny(now)).total_seconds() / 60.0


__all__ = [
    "NY", "SESSION_OPEN", "DEFAULT_RANGE_MINUTES", "ensure_ny", "session_open_dt",
    "drop_unclosed_1m", "aggregate_to_5m", "session_vwap_series", "opening_range",
    "opening_range_mid", "average_volume", "volume_ratio", "minutes_until",
]
