# -*- coding: utf-8 -*-
"""Setup scanner: trend + momentum + pullback (ТЗ §4), graded A/B/C (§5).

Consumes per-symbol 1-minute candles (as downloaded for the backtest) and
resamples them to 5m/15m/30m. Emits a single signal per symbol per day when a
valid setup forms during the impulse window of the main session.

Session model (US stocks on the challenge terminal):
  13:30-19:55 UTC. First ~10-15 min are observation-only; the momentum bar must
  print within the first 60-90 minutes; entries are allowed only there.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from challenge.manual.sr_zones import detect_sr_zones, check_proximity

SESSION_START_UTC = dt.time(13, 30)
SESSION_END_UTC = dt.time(19, 55)
IMPULSE_WINDOW_MIN = 60
IMPULSE_WINDOW_MAX = 90
NO_ENTRY_FIRST_MIN = 12


def ema(values, period: int):
    """Exponential moving average over a list of floats."""
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def resample(candles, minutes: int):
    """1-min -> N-min bars (open=first open, high=max, low=min, close=last close)."""
    out, cur = [], None
    for c in sorted(candles, key=lambda x: x["time"]):
        if cur is None:
            cur = {"open": c["open"], "high": c["high"], "low": c["low"],
                   "close": c["close"], "time": c["time"], "volume": c.get("volume", 0.0)}
        elif c["time"] < cur["time"] + minutes * 60:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["volume"] += c.get("volume", 0.0)
        else:
            out.append(cur)
            cur = {"open": c["open"], "high": c["high"], "low": c["low"],
                   "close": c["close"], "time": c["time"], "volume": c.get("volume", 0.0)}
    if cur:
        out.append(cur)
    return out


def _utc_time(ts: int) -> dt.time:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).timetz().replace(tzinfo=None)


def bars_of_day(candles, date) -> list:
    day = []
    for c in candles:
        utc = dt.datetime.fromtimestamp(c["time"], dt.timezone.utc)
        if utc.date() == date:
            day.append(c)
    return day


def atr(candles, period: int = 14) -> float:
    """Average true range over the given candles (needs >= period+1 bars)."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    prev_close = candles[0]["close"]
    for c in candles[1:]:
        tr = max(c["high"] - c["low"],
                 abs(c["high"] - prev_close),
                 abs(c["low"] - prev_close))
        trs.append(tr)
        prev_close = c["close"]
    return sum(trs[-period:]) / period


def check_news_red_zone(now: dt.datetime, zones) -> bool:
    """True if `now` is within +/-30 min of a red-zone news event (ТЗ §4.1).
    `zones` is a list of "HH:MM" UTC event times, e.g. ["13:30"]."""
    t = now.timetz().replace(tzinfo=None)
    for z in zones or []:
        try:
            ev = dt.datetime.strptime(z, "%H:%M").time()
        except ValueError:
            continue
        ev_ts = (dt.timedelta(hours=ev.hour, minutes=ev.minute)).total_seconds()
        t_ts = (dt.timedelta(hours=t.hour, minutes=t.minute)).total_seconds()
        if abs(t_ts - ev_ts) <= 30 * 60:
            return True
    return False


@dataclass
class Setup:
    symbol: str
    date: str
    bias: str                          # long | short | none
    grade: str                         # A | B | C | none
    impulse_bar: Optional[dict] = None
    pullback_bars: list = field(default_factory=list)
    signal_bar: Optional[dict] = None
    trend15: str = ""                  # up | down | flat
    trend30: str = ""
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    rr: float = 0.0
    no_go: list = field(default_factory=list)

    @property
    def tradable(self) -> bool:
        return self.bias != "none" and self.grade in ("A", "B") and not self.no_go


def _higher_highs_lower_lows(closes: list, n: int = 20) -> tuple[bool, bool]:
    """Check HH/HL structure over the last `n` closes (ТЗ §4.2)."""
    if len(closes) < n:
        return False, False
    seg = closes[-n:]
    highs = max(seg[len(seg) // 2:])
    lows = min(seg[len(seg) // 2:])
    first_half = seg[:len(seg) // 2]
    hh = all(first_half[i] < first_half[i + 1] for i in range(len(first_half) - 1))
    hl = all(first_half[i] < first_half[i + 1] for i in range(len(first_half) - 1))
    return hh, hl


def _trend(day_bars, ema_period: int = 20) -> str:
    if len(day_bars) < ema_period + 10:
        return "flat"
    closes = [b["close"] for b in day_bars]
    e = ema(closes, ema_period)
    cur = e[-1]
    prev = e[-6] if len(e) >= 6 else e[0]
    slope = (cur - prev) / prev if prev else 0.0
    price = closes[-1]
    above = price > cur
    if slope > 0.0005 and above:
        return "up"
    if slope < -0.0005 and not above:
        return "down"
    return "flat"


def _impulse_candidates(bars5, avg_range, avg_vol, window_start_idx, window_end_idx,
                        trend: str) -> list:
    """Momentum bars inside the impulse window (ТЗ §4.3)."""
    out = []
    for b in bars5[window_start_idx:window_end_idx]:
        rng = b["high"] - b["low"]
        body = abs(b["close"] - b["open"])
        if avg_range <= 0 or avg_vol <= 0:
            continue
        if rng < 1.3 * avg_range:
            continue
        if body < 0.5 * rng:
            continue
        if b.get("volume", 0) < 1.3 * avg_vol:
            continue
        if trend == "up" and b["close"] < b["low"] + 0.66 * rng:
            continue
        if trend == "down" and b["close"] > b["high"] - 0.66 * rng:
            continue
        out.append(b)
    return out


def _pullback(bars5, impulse, avg_range, idx_of, trend: str):
    """Scan the bars right after the impulse for a 38-61.8% pullback to the
    5m EMA20 or the impulse high/low (ТЗ §4.4). Returns (bars, retrace_pct,
    depth_ok, time_ok)."""
    start = idx_of(impulse["time"])
    if start is None:
        return [], 0.0, False, False
    closes = [b["close"] for b in bars5[:start + 1]]
    e = ema(closes, 20)
    ema_now = e[-1]
    i_high = impulse["high"]
    i_low = impulse["low"]
    i_range = i_high - i_low
    pull = []
    for b in bars5[start + 1:start + 11]:  # max 10 bars (50 min)
        if b["time"] <= impulse["time"]:
            continue
        pull.append(b)
        if trend == "up":
            depth = (i_high - b["low"]) / i_range if i_range else 0.0
            if b["low"] < i_low:
                return pull, depth, False, True   # impulse low broken
            if depth >= 0.382:
                return pull, depth, depth <= 0.618, len(pull) <= 10
        else:
            depth = (b["high"] - i_low) / i_range if i_range else 0.0
            if b["high"] > i_high:
                return pull, depth, False, True
            if depth >= 0.382:
                return pull, depth, depth <= 0.618, len(pull) <= 10
    return pull, 0.0, False, True


def _is_pinbar(b, trend: str) -> bool:
    rng = b["high"] - b["low"]
    body = abs(b["close"] - b["open"])
    if rng <= 0:
        return False
    upper = b["high"] - max(b["open"], b["close"])
    lower = min(b["open"], b["close"]) - b["low"]
    if body < 0.45 * rng and max(upper, lower) > 1.6 * body:
        if trend == "up":
            return lower > 1.6 * body and b["close"] >= b["low"] + 0.6 * rng
        if trend == "down":
            return upper > 1.6 * body and b["close"] <= b["low"] + 0.4 * rng
    return False


def _is_engulfing(prev, cur, trend: str) -> bool:
    if trend == "up":
        return (cur["close"] > cur["open"] and prev["close"] < prev["open"]
                and cur["open"] <= prev["close"] and cur["close"] >= prev["open"])
    if trend == "down":
        return (cur["close"] < cur["open"] and prev["close"] > prev["open"]
                and cur["open"] >= prev["close"] and cur["close"] <= prev["open"])
    return False


def _signal(bars5, pull_bars, idx_of, avg_vol, trend: str):
    """Entry signal after the pullback: pin-bar/engulfing + close beyond the
    pullback extreme (ТЗ §4.5). Returns (bar, ok)."""
    if not pull_bars:
        return None, False
    last = pull_bars[-1]
    i = idx_of(last["time"])
    if i is None or i + 1 >= len(bars5):
        return None, False
    signal = bars5[i + 1]
    prev = bars5[i]
    vol_ok = signal.get("volume", 0) >= 0.8 * avg_vol
    if trend == "up":
        extreme = max(b["high"] for b in pull_bars)
        close_ok = signal["close"] > extreme
    else:
        extreme = min(b["low"] for b in pull_bars)
        close_ok = signal["close"] < extreme
    # ТЗ §4.5: entry signal = close beyond the pullback line OR a pin-bar OR an
    # engulfing bar at the pullback extreme (any one of the three is enough).
    if not (close_ok or _is_pinbar(signal, trend) or _is_engulfing(prev, signal, trend)):
        return signal, False
    if not vol_ok:
        return signal, False
    return signal, True


def _grade(trend15, trend30, impulse, pull_retrace, signal_ok, atr_normal,
           news: bool) -> tuple[str, list]:
    """ТЗ §5.1: A/B/C grading + explicit NO-GO list.

    IMPORTANT (2026-08-21, 24w backtest): the A/B split by retrace depth is
    DESCRIPTIVE ONLY — it does not predict outcomes (A avgR -0.073 vs B +0.029,
    and the ordering inverts with regime). Nothing downstream may gate on grade:
    risk profiles allow both A and B (risk.only_a=False everywhere). The NO-GO
    list is the real filter (news / dead-day ATR / trend conflict / signal).
    """
    no_go = []
    if news:
        no_go.append("red-zone news")
    if not atr_normal:
        no_go.append("abnormal daily ATR")
    if trend15 != trend30:
        no_go.append("15m/30m trend conflict")
    if trend15 == "flat":
        no_go.append("15m flat")
    if impulse is None:
        no_go.append("no impulse bar")
    if not signal_ok:
        no_go.append("no entry signal")
    if pull_retrace is None:
        no_go.append("pullback too deep")
    if no_go:
        return "C", no_go
    if trend15 == trend30 and trend15 != "flat" and impulse is not None \
            and 0.382 <= pull_retrace <= 0.50 and signal_ok:
        return "A", []
    return "B", []


def scan_setup(symbol: str, date, candles_1m, session_start_utc=SESSION_START_UTC,
               cfg=None) -> Setup:
    """Run the full setup scan for one symbol on one date (ТЗ §4)."""
    cfg = cfg or {}
    news_zones = cfg.get("news_red_zone_utc") or []
    day = bars_of_day(candles_1m, date)
    if len(day) < 40:
        return Setup(symbol, str(date), "none", "none", no_go=["insufficient data"])

    bars5 = resample(day, 5)
    bars15 = resample(day, 15)
    bars30 = resample(day, 30)

    trend15 = _trend(bars15)
    trend30 = _trend(bars30)

    # Daily activity filter (ТЗ §4.1): how active today is vs prior sessions.
    # The comparison window is clamped to the elapsed session time, so the rule
    # is honest live (a normal day early in the session is compared with what
    # prior days had moved by that same time of day) and equals the full-session
    # ratio in backtests (elapsed >= full session => full window on both sides).
    # Dead days (range < atr_min_ratio of normal) are NO-GO: 24w backtest data
    # showed ~100% of the strategy's losses concentrate on atr_ratio < 0.7.
    prior = [c for c in candles_1m if c["time"] < dt.datetime(
        date.year, date.month, date.day, tzinfo=dt.timezone.utc).timestamp()]
    prior_days = {}
    for c in prior:
        d = dt.datetime.fromtimestamp(c["time"], dt.timezone.utc).date()
        prior_days.setdefault(d, []).append(c)
    sess_start_dt = dt.datetime.combine(date, session_start_utc, tzinfo=dt.timezone.utc)
    sess_len_min = (SESSION_END_UTC.hour * 60 + SESSION_END_UTC.minute) - \
                   (session_start_utc.hour * 60 + session_start_utc.minute)
    last_ts = max((c["time"] for c in day), default=0)
    elapsed_min = max(1.0, min(sess_len_min,
                               (last_ts - sess_start_dt.timestamp()) / 60.0))

    def _range_in_window(cs, d):
        s = dt.datetime.combine(d, session_start_utc, tzinfo=dt.timezone.utc).timestamp()
        w = [c for c in cs if s <= c["time"] <= s + elapsed_min * 60]
        if len(w) < 5:
            return None
        return max(c["high"] for c in w) - min(c["low"] for c in w)

    atr_today = _range_in_window(day, date) or 0.0
    atr_hist = [r for r in (_range_in_window(v, d) for d, v in prior_days.items()) if r]
    if atr_hist and atr_today > 0:
        atr_mean = sum(atr_hist[-20:]) / min(20, len(atr_hist))
        lo = float(cfg.get("atr_min_ratio", 0.70))
        hi = float(cfg.get("atr_max_ratio", 2.5))
        atr_normal = atr_mean > 0 and lo <= atr_today / atr_mean <= hi
    else:
        atr_normal = True

    # Impulse window inside the session (first 60-90 minutes).
    t_start = dt.datetime.combine(date, session_start_utc, tzinfo=dt.timezone.utc)
    t_end = t_start + dt.timedelta(minutes=IMPULSE_WINDOW_MAX)
    def idx_of(ts):
        for i, b in enumerate(bars5):
            if b["time"] == ts:
                return i
        return None

    win_lo = next((i for i, b in enumerate(bars5)
                   if b["time"] >= t_start.timestamp() + NO_ENTRY_FIRST_MIN * 60), 0)
    win_hi = next((i for i, b in enumerate(bars5)
                   if b["time"] > t_end.timestamp()), len(bars5))

    closes5 = [b["close"] for b in bars5]
    avg_range = sum(b["high"] - b["low"] for b in bars5[-20:]) / min(20, len(bars5))
    avg_vol = sum(b.get("volume", 0) for b in bars5[-20:]) / min(20, len(bars5))

    setup = Setup(symbol, str(date), "none", "none", trend15=trend15, trend30=trend30)
    trend = trend15 if trend15 == trend30 else ("up" if trend15 != "flat" else "flat")

    impulses = _impulse_candidates(bars5, avg_range, avg_vol, win_lo, win_hi, trend)
    impulse = impulses[0] if impulses else None

    pull_bars, retrace, depth_ok, time_ok = [], 0.0, False, True
    if impulse is not None:
        pull_bars, retrace, depth_ok, time_ok = _pullback(bars5, impulse, avg_range,
                                                          idx_of, trend)
    signal_bar, signal_ok = (None, False)
    if impulse is not None and pull_bars:
        signal_bar, signal_ok = _signal(bars5, pull_bars, idx_of, avg_vol, trend)

    # News check at signal time (or, failing that, at the end of the window).
    check_ts = signal_bar["time"] if signal_bar else (t_end.timestamp() if impulse else t_start.timestamp())
    news = check_news_red_zone(dt.datetime.fromtimestamp(check_ts, dt.timezone.utc), news_zones)

    grade, no_go = _grade(trend15, trend30, impulse,
                          retrace if depth_ok else (None if not pull_bars else 1.0),
                          signal_ok, atr_normal, news)

    # Signal dead zone (2026-08-21, 24w/411-setup backtest): signals printing
    # 60-69 min after the open are the only consistently negative bucket
    # (avgR -0.324, n=44; neighbours 50-59 +0.856 and 80-89 +1.014). Excluding
    # them lifts avgR +0.295 -> +0.370 with pace unchanged (~20 days at $5
    # risk) and improves every quarter. Configurable via signal_dead_zone.
    if signal_bar is not None:
        sig_min = (signal_bar["time"] - t_start.timestamp()) / 60.0
        dz = cfg.get("signal_dead_zone")
        if dz and len(dz) == 2 and dz[0] <= sig_min <= dz[1]:
            no_go.append(f"signal dead zone {dz[0]}-{dz[1]} min")

    setup.impulse_bar = impulse
    setup.pullback_bars = pull_bars
    setup.signal_bar = signal_bar
    setup.grade = grade
    setup.no_go = no_go

    # Direction & levels (ТЗ §4.6): stop behind the pullback extreme + 0.5*ATR5,
    # target = entry +/- target_rr*risk. target_rr defaults to 2.0 (ТЗ); the live
    # config sets 3.5 — a far take-profit beats the 2R cap on the 24w/411-setup
    # backtest (avgR +0.295 vs -0.021 for the old 50%@1R->BE->2R plan, 2026-08-21).
    if impulse is not None and signal_bar is not None and signal_ok and not no_go:
        if trend == "up":
            bias = "long"
            extreme = min(b["low"] for b in pull_bars) if pull_bars else impulse["low"]
            entry = signal_bar["close"]
            atr5 = atr(day, 14)
            stop = extreme - 0.5 * atr5
            setup.entry, setup.stop = round(entry, 4), round(stop, 4)
        else:
            bias = "short"
            extreme = max(b["high"] for b in pull_bars) if pull_bars else impulse["high"]
            entry = signal_bar["close"]
            atr5 = atr(day, 14)
            stop = extreme + 0.5 * atr5
            setup.entry, setup.stop = round(entry, 4), round(stop, 4)
        # ТЗ §4.6 guard: reject degenerate levels (stop on the wrong side of
        # entry, or entry == stop). Such a setup must never reach alerts/tests:
        # long requires stop < entry < target, short requires target < entry <
        # stop. Keeps grade but marks NO-GO so `tradable` is False.
        sane = (stop < entry) if bias == "long" else (entry < stop)
        if sane:
            risk = (entry - stop) if bias == "long" else (stop - entry)
            target_rr = float(cfg.get("target_rr", 2.0))
            if bias == "long":
                setup.target = round(entry + target_rr * risk, 4)
            else:
                setup.target = round(entry - target_rr * risk, 4)
            setup.rr = round(abs(setup.target - entry) / risk, 2)
            setup.bias = bias

            # RESEARCH 2026-08-22: S/R proximity filter (us_stocks audit §5.2)
            # Reject signals too close to key support/resistance zones.
            # This prevents entering right at a level that may cap the move.
            # Default 0 = disabled; set sr_proximity_buffer_usd in cfg to enable.
            sr_buffer = float(cfg.get("sr_proximity_buffer_usd", 0))
            if sr_buffer > 0 and bias in ("long", "short"):
                sr_zones = detect_sr_zones(candles_1m, date)
                sr_ok, sr_reason = check_proximity(
                    entry, stop, setup.target, bias, sr_zones, sr_buffer)
                if not sr_ok:
                    setup.bias = "none"
                    setup.no_go.append(f"S/R proximity: {sr_reason}")
        else:
            setup.bias = "none"
            setup.no_go.append("degenerate levels")
    return setup