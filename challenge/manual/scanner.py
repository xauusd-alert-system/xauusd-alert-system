# -*- coding: utf-8 -*-
"""Causal setup scanner for the US Stocks Headliners manual workflow.

The scanner reads supplied candles and returns an analytical candidate. It never
opens a terminal, sends an alert, or places/changes/closes an order. Every call
can provide ``as_of_ts`` so historical replay uses only information available at
that instant.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

LEGACY_SESSION_START_UTC = dt.time(13, 30)
LEGACY_SESSION_END_UTC = dt.time(19, 55)


@dataclass
class Setup:
    symbol: str
    date: str
    bias: str
    grade: str
    impulse_bar: Optional[dict] = None
    pullback_bars: list = field(default_factory=list)
    signal_bar: Optional[dict] = None
    trend15: str = ""
    trend30: str = ""
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    rr: float = 0.0
    no_go: list = field(default_factory=list)
    as_of_utc: int = 0

    @property
    def tradable(self) -> bool:
        return self.bias != "none" and self.grade in ("A", "B") and not self.no_go


def ema(values, period: int):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def resample(candles, minutes: int):
    """Aggregate ordered 1-minute candles without fabricating missing bars."""
    out, current = [], None
    for candle in sorted(candles, key=lambda item: int(item["time"])):
        if current is None:
            current = {"open": candle["open"], "high": candle["high"], "low": candle["low"],
                       "close": candle["close"], "time": int(candle["time"]),
                       "volume": float(candle.get("volume", 0.0))}
        elif int(candle["time"]) < current["time"] + minutes * 60:
            current["high"] = max(current["high"], candle["high"])
            current["low"] = min(current["low"], candle["low"])
            current["close"] = candle["close"]
            current["volume"] += float(candle.get("volume", 0.0))
        else:
            out.append(current)
            current = {"open": candle["open"], "high": candle["high"], "low": candle["low"],
                       "close": candle["close"], "time": int(candle["time"]),
                       "volume": float(candle.get("volume", 0.0))}
    if current:
        out.append(current)
    return out


def atr(candles, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    previous = float(candles[0]["close"])
    values = []
    for candle in candles[1:]:
        high, low = float(candle["high"]), float(candle["low"])
        values.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = float(candle["close"])
    return sum(values[-period:]) / period


def _time(value: str) -> dt.time:
    hour, minute = map(int, value.split(":"))
    return dt.time(hour, minute)


def _offset(cfg: dict) -> dt.tzinfo:
    return dt.timezone(dt.timedelta(minutes=int(cfg.get("session_timezone_offset_minutes", 0))))


def session_bounds(date: dt.date, cfg: dict | None = None,
                   legacy_start_utc: dt.time | None = None) -> tuple[int, int]:
    """Return UTC timestamps for the configured local session, including midnight crossover."""
    cfg = cfg or {}
    if cfg.get("session_start_local"):
        zone = _offset(cfg)
        start_local = dt.datetime.combine(date, _time(cfg["session_start_local"]), tzinfo=zone)
        end_local = dt.datetime.combine(date, _time(cfg.get("session_end_local", "00:55")), tzinfo=zone)
        if end_local <= start_local:
            end_local += dt.timedelta(days=1)
        return int(start_local.astimezone(dt.timezone.utc).timestamp()), int(end_local.astimezone(dt.timezone.utc).timestamp())
    start = legacy_start_utc or LEGACY_SESSION_START_UTC
    start_dt = dt.datetime.combine(date, start, tzinfo=dt.timezone.utc)
    end_dt = dt.datetime.combine(date, LEGACY_SESSION_END_UTC, tzinfo=dt.timezone.utc)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def bars_of_day(candles, date) -> list:
    """Legacy UTC-day helper retained for compatibility with historical tests."""
    return [c for c in candles if dt.datetime.fromtimestamp(int(c["time"]), dt.timezone.utc).date() == date]


def _trend(bars, ema_period: int = 20) -> str:
    if len(bars) < ema_period + 10:
        return "flat"
    closes = [float(bar["close"]) for bar in bars]
    values = ema(closes, ema_period)
    current, earlier = values[-1], values[-6]
    slope = (current - earlier) / earlier if earlier else 0.0
    above = closes[-1] > current
    if slope > 0.0005 and above:
        return "up"
    if slope < -0.0005 and not above:
        return "down"
    return "flat"


def _structure_ok(bars, trend: str, lookback: int = 20) -> bool:
    if len(bars) < lookback:
        return False
    sample = bars[-lookback:]
    mids = len(sample) // 2
    first, second = sample[:mids], sample[mids:]
    if not first or not second:
        return False
    if trend == "up":
        return max(float(b["high"]) for b in second) > max(float(b["high"]) for b in first) and \
               min(float(b["low"]) for b in second) > min(float(b["low"]) for b in first)
    if trend == "down":
        return min(float(b["low"]) for b in second) < min(float(b["low"]) for b in first) and \
               max(float(b["high"]) for b in second) < max(float(b["high"]) for b in first)
    return False


def _pinbar(bar, trend: str) -> bool:
    high, low, opening, close = map(float, (bar["high"], bar["low"], bar["open"], bar["close"]))
    span, body = high - low, abs(close - opening)
    if span <= 0 or body >= 0.45 * span:
        return False
    upper, lower = high - max(opening, close), min(opening, close) - low
    return (trend == "up" and lower > 1.6 * body and close >= low + 0.6 * span) or \
           (trend == "down" and upper > 1.6 * body and close <= low + 0.4 * span)


def _engulfing(previous, current, trend: str) -> bool:
    po, pc = float(previous["open"]), float(previous["close"])
    co, cc = float(current["open"]), float(current["close"])
    if trend == "up":
        return cc > co and pc < po and co <= pc and cc >= po
    return cc < co and pc > po and co >= pc and cc <= po


def _red_zone(ts: int, cfg: dict) -> bool:
    zones = cfg.get("news_red_zone_local") or cfg.get("news_red_zone_utc") or []
    if not zones:
        return False
    if cfg.get("news_red_zone_local"):
        moment = dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone(_offset(cfg))
    else:
        moment = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    now_seconds = moment.hour * 3600 + moment.minute * 60
    buffer_seconds = int(cfg.get("news_buffer_minutes", 30)) * 60
    for item in zones:
        try:
            point = _time(str(item))
        except ValueError:
            continue
        event_seconds = point.hour * 3600 + point.minute * 60
        if abs(now_seconds - event_seconds) <= buffer_seconds:
            return True
    return False


def _activity_normal(session_bars, historical, session_start_ts: int,
                     as_of_ts: int, cfg: dict) -> bool:
    if len(session_bars) < 5:
        return False
    elapsed = max(1, as_of_ts - session_start_ts)
    current_range = max(float(c["high"]) for c in session_bars) - min(float(c["low"]) for c in session_bars)
    if current_range <= 0:
        return False
    by_date = {}
    for candle in historical:
        if int(candle["time"]) >= session_start_ts:
            continue
        date_key = dt.datetime.fromtimestamp(int(candle["time"]), dt.timezone.utc).date()
        by_date.setdefault(date_key, []).append(candle)
    previous_ranges = []
    for day, candles in by_date.items():
        start, _ = session_bounds(day, cfg)
        window = [c for c in candles if start <= int(c["time"]) <= start + elapsed]
        if len(window) >= 5:
            previous_ranges.append(max(float(c["high"]) for c in window) - min(float(c["low"]) for c in window))
    if not previous_ranges:
        return True
    baseline = sum(previous_ranges[-20:]) / min(20, len(previous_ranges))
    ratio = current_range / baseline if baseline else 0.0
    return float(cfg.get("atr_min_ratio", 0.70)) <= ratio <= float(cfg.get("atr_max_ratio", 2.50))


def scan_setup(symbol: str, date, candles_1m, session_start_utc: dt.time | None = None,
               cfg: dict | None = None, as_of_ts: int | None = None) -> Setup:
    """Scan one manual candidate using candles available no later than ``as_of_ts``."""
    cfg = cfg or {}
    if not candles_1m:
        return Setup(symbol, str(date), "none", "none", no_go=["insufficient data"])
    as_of_ts = int(as_of_ts or max(int(c["time"]) for c in candles_1m))
    start_ts, end_ts = session_bounds(date, cfg, session_start_utc)
    usable = [c for c in candles_1m if int(c["time"]) <= as_of_ts]
    session = [c for c in usable if start_ts <= int(c["time"]) <= min(as_of_ts, end_ts)]
    base = Setup(symbol, str(date), "none", "none", as_of_utc=as_of_ts)
    if len(session) < 40:
        base.no_go.append("insufficient session data")
        return base

    bars5 = resample(session, 5)
    historical = [c for c in usable if int(c["time"]) <= as_of_ts]
    bars15_all = resample(historical, 15)
    bars30_all = resample(historical, 30)
    trend15, trend30 = _trend(bars15_all), _trend(bars30_all)
    base.trend15, base.trend30 = trend15, trend30
    if trend15 != trend30 or trend15 == "flat":
        base.no_go.append("15m/30m trend conflict or flat")
        return base
    trend = trend15
    if not _structure_ok(bars15_all, trend):
        base.no_go.append("missing 15m structure")
        return base
    if _red_zone(as_of_ts, cfg):
        base.no_go.append("red-zone news")
        return base
    if cfg.get("calendar_status", "verified") == "unknown":
        base.no_go.append("calendar status unknown")
        return base
    if not _activity_normal(session, historical, start_ts, as_of_ts, cfg):
        base.no_go.append("abnormal daily ATR")
        return base

    no_entry = int(cfg.get("no_entry_first_minutes", 15)) * 60
    cutoff = int(cfg.get("entry_cutoff_minutes", 90)) * 60
    max_retrace_bars = 10
    min_range = float(cfg.get("impulse_range_min_multiple", 1.50))
    min_volume = float(cfg.get("impulse_volume_min_multiple", 1.30))
    signal_volume = float(cfg.get("signal_volume_min_multiple", 0.80))
    pull_min = float(cfg.get("pullback_min_ratio", 0.382))
    pull_max = float(cfg.get("pullback_max_ratio", 0.618))

    for impulse_index, impulse in enumerate(bars5):
        elapsed = int(impulse["time"]) - start_ts
        if elapsed < no_entry or elapsed > cutoff or impulse_index < 20:
            continue
        prior = bars5[max(0, impulse_index - 20):impulse_index]
        avg_range = sum(float(b["high"]) - float(b["low"]) for b in prior) / len(prior)
        avg_volume = sum(float(b.get("volume", 0.0)) for b in prior) / len(prior)
        span = float(impulse["high"]) - float(impulse["low"])
        body = abs(float(impulse["close"]) - float(impulse["open"]))
        close_location = (float(impulse["close"]) - float(impulse["low"])) / span if span else 0.0
        direction_ok = (trend == "up" and close_location >= 0.66) or (trend == "down" and close_location <= 0.34)
        if avg_range <= 0 or avg_volume <= 0 or span < min_range * avg_range or \
           body < 0.50 * span or float(impulse.get("volume", 0.0)) < min_volume * avg_volume or not direction_ok:
            continue

        pullback = []
        depth = None
        invalid = False
        for bar in bars5[impulse_index + 1:impulse_index + 1 + max_retrace_bars]:
            pullback.append(bar)
            if trend == "up":
                depth = (float(impulse["high"]) - float(bar["low"])) / span
                invalid = float(bar["low"]) < float(impulse["low"])
            else:
                depth = (float(bar["high"]) - float(impulse["low"])) / span
                invalid = float(bar["high"]) > float(impulse["high"])
            if invalid or depth >= pull_min:
                break
        if not pullback or invalid or depth is None or not pull_min <= depth <= pull_max:
            continue
        signal_index = impulse_index + 1 + len(pullback)
        if signal_index >= len(bars5):
            continue
        signal, previous = bars5[signal_index], bars5[signal_index - 1]
        if trend == "up":
            extreme = max(float(b["high"]) for b in pullback)
            signal_ok = float(signal["close"]) > extreme or _pinbar(signal, trend) or _engulfing(previous, signal, trend)
        else:
            extreme = min(float(b["low"]) for b in pullback)
            signal_ok = float(signal["close"]) < extreme or _pinbar(signal, trend) or _engulfing(previous, signal, trend)
        if not signal_ok or float(signal.get("volume", 0.0)) < signal_volume * avg_volume:
            continue
        if int(signal["time"]) - start_ts > cutoff:
            continue

        atr5 = atr(bars5[:signal_index + 1], 14)
        buffer = float(cfg.get("stop_atr_buffer_multiple", 0.50)) * atr5
        entry = float(signal["close"])
        if trend == "up":
            stop = min(float(b["low"]) for b in pullback) - buffer
            bias = "long"
        else:
            stop = max(float(b["high"]) for b in pullback) + buffer
            bias = "short"
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        final_r = float((cfg.get("outcome_plan") or {}).get("final_target_r", 2.0))
        target = entry + final_r * risk if bias == "long" else entry - final_r * risk
        strong_impulse = span >= 1.5 * avg_range and float(impulse.get("volume", 0.0)) >= 1.5 * avg_volume
        strong_pullback = depth <= 0.50
        grade = "A" if strong_impulse and strong_pullback else "B"
        return Setup(symbol, str(date), bias, grade, impulse_bar=impulse,
                     pullback_bars=pullback, signal_bar=signal, trend15=trend15,
                     trend30=trend30, entry=round(entry, 4), stop=round(stop, 4),
                     target=round(target, 4), rr=round(final_r, 2), as_of_utc=as_of_ts)

    base.no_go.append("no valid impulse-pullback-signal candidate")
    return base
