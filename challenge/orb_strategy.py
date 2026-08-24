"""Opening Range Breakout (ORB) strategy for UTEx challenge (TSLA, AAPL, NVDA, AMZN, META).

Spec:
- Tickers: TSLA, AAPL, NVDA, AMZN, META rotation by premarket volume
- Timeframes: 5-min for range, 1-min for entry
- 9:30-9:45 ET: collect 3x 5-min candles, fix ORB_high, ORB_low
- Filters: 0.3%-1.5% price range, gap >3% skip, volume first 5-min <1.5x 20d avg skip, earnings day skip
- 9:45-10:30 ET: monitor 1-min candles, breakout ORB_high/low, close beyond range, hold next candle, trade only gap direction
- After 10:30 no new entries, manage positions, close all before 15:30 ET
- Risk: 1% ($10) jitter 0.7-1.3%, SL under low of breakout 5-min candle (long) / above high (short), TP 2R, shares = $10 / SL$, max 2/day

All timing constants inside class, seed optional.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, date, time as dt_time, timezone, timedelta
from typing import Dict, List, Optional, Tuple


def _parse_hm(hm: str) -> dt_time:
    h, m = map(int, hm.split(":"))
    return dt_time(h, m)


def _minutes_of_day(t: dt_time) -> int:
    return t.hour * 60 + t.minute


def _minutes_from_dt(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


@dataclass
class ORBSignal:
    symbol: str
    bias: str  # long/short
    entry: float
    stop: float
    tp: float
    orb_high: float
    orb_low: float
    breakout_candle_high: float
    breakout_candle_low: float
    gap_pct: float
    range_pct: float
    volume_ratio: float
    session_bucket: str = "normal"
    timestamp: Optional[datetime] = None


@dataclass
class ORBRange:
    symbol: str
    high: Optional[float] = None
    low: Optional[float] = None
    candles_5min: List[Dict] = field(default_factory=list)
    first_candle_volume: Optional[float] = None
    gap_pct: Optional[float] = None
    prev_close: Optional[float] = None
    open_price: Optional[float] = None
    filtered_out: bool = False
    filter_reason: Optional[str] = None


class ORBStrategy:
    """ORB 5-min range 9:30-9:45 ET, 1-min entry 9:45-10:30 ET."""

    # Time windows ET (treated as UTC for simplicity, but configurable)
    RANGE_START = "09:30"
    RANGE_END = "09:45"
    ENTRY_START = "09:45"
    ENTRY_END = "10:30"
    CLOSE_ALL = "15:30"

    # Filters
    RANGE_MIN_PCT = 0.003  # 0.3%
    RANGE_MAX_PCT = 0.015  # 1.5%
    GAP_MAX_PCT = 0.03  # 3%
    VOLUME_MIN_RATIO = 1.5  # first 5-min vs 20d avg

    # Risk
    RISK_BASE_USD = 10.0
    RISK_JITTER_MIN = 0.007
    RISK_JITTER_MAX = 0.013
    TP_R_MULT = 2.0

    # Tickers
    DEFAULT_TICKERS = ["TSLA", "AAPL", "NVDA", "AMZN", "META"]

    def __init__(
        self,
        cfg: Optional[Dict] = None,
        tickers: Optional[List[str]] = None,
        seed: Optional[int] = None,
        earnings_calendar: Optional[List[Dict]] = None,
    ):
        self._rng = random.Random(seed)
        self.cfg = cfg or {}
        self.tickers = tickers or self.DEFAULT_TICKERS

        # Config overrides
        if cfg:
            stealth = cfg.get("stealth", {}) or {}
            # Tickers from stealth config
            if stealth.get("challenge_tickers"):
                self.tickers = stealth["challenge_tickers"]
            # Risk jitter
            if stealth.get("risk_jitter_range"):
                self.RISK_JITTER_MIN, self.RISK_JITTER_MAX = stealth["risk_jitter_range"]
            # Session windows
            if stealth.get("et_range_window"):
                self.RANGE_START, self.RANGE_END = stealth["et_range_window"]
            if stealth.get("et_entry_window"):
                self.ENTRY_START, self.ENTRY_END = stealth["et_entry_window"]
            if stealth.get("et_close_all_time"):
                self.CLOSE_ALL = stealth["et_close_all_time"]

            # ORB filters from challenge config if present
            challenge = cfg.get("challenge", {}) or {}
            orb_cfg = challenge.get("orb", {}) or {}
            if orb_cfg.get("range_min_pct"):
                self.RANGE_MIN_PCT = float(orb_cfg["range_min_pct"])
            if orb_cfg.get("range_max_pct"):
                self.RANGE_MAX_PCT = float(orb_cfg["range_max_pct"])
            if orb_cfg.get("gap_max_pct"):
                self.GAP_MAX_PCT = float(orb_cfg["gap_max_pct"])
            if orb_cfg.get("volume_min_ratio"):
                self.VOLUME_MIN_RATIO = float(orb_cfg["volume_min_ratio"])

        self.RANGE_START_MIN = _minutes_of_day(_parse_hm(self.RANGE_START))
        self.RANGE_END_MIN = _minutes_of_day(_parse_hm(self.RANGE_END))
        self.ENTRY_START_MIN = _minutes_of_day(_parse_hm(self.ENTRY_START))
        self.ENTRY_END_MIN = _minutes_of_day(_parse_hm(self.ENTRY_END))
        self.CLOSE_ALL_MIN = _minutes_of_day(_parse_hm(self.CLOSE_ALL))

        self.earnings_calendar = earnings_calendar or []
        self._earnings_by_ticker: Dict[str, set] = {}
        self._rebuild_earnings()

        # Per-symbol ORB ranges
        self._ranges: Dict[str, ORBRange] = {}
        self._session_date: Optional[date] = None
        self._avg_volumes_20d: Dict[str, float] = {}  # 20d avg volume per symbol

        # Track breakout confirmation: need close beyond range + hold next candle
        self._pending_breakouts: Dict[str, Dict] = {}

    def _rebuild_earnings(self):
        self._earnings_by_ticker.clear()
        for ev in self.earnings_calendar:
            ticker = str(ev.get("ticker", "")).upper()
            d = ev.get("date")
            parsed: Optional[date] = None
            if isinstance(d, date) and not isinstance(d, datetime):
                parsed = d
            elif isinstance(d, datetime):
                parsed = d.date()
            elif isinstance(d, str):
                try:
                    parsed = datetime.fromisoformat(d).date()
                except Exception:
                    continue
            if ticker and parsed:
                self._earnings_by_ticker.setdefault(ticker, set()).add(parsed)

    def is_earnings_day(self, ticker: str, check_date: date) -> bool:
        dates = self._earnings_by_ticker.get(ticker.upper())
        return check_date in dates if dates else False

    def _reset_if_new_day(self, now: datetime):
        if self._session_date != now.date():
            self._session_date = now.date()
            self._ranges.clear()
            self._pending_breakouts.clear()

    def update_avg_volume(self, symbol: str, volumes_20d: List[float]):
        """Set 20d avg volume for symbol (from historical data)."""
        if volumes_20d:
            self._avg_volumes_20d[symbol] = sum(volumes_20d) / len(volumes_20d)

    def select_tickers_by_premarket_volume(self, premarket_volumes: Dict[str, float], top_n: int = 3) -> List[str]:
        """Rotate tickers by premarket volume, not one ticker each day."""
        # Sort by volume descending
        sorted_tickers = sorted(premarket_volumes.items(), key=lambda x: x[1], reverse=True)
        selected = [t for t, _ in sorted_tickers[:top_n]]
        # Ensure at least default tickers if not enough
        if len(selected) < top_n:
            for t in self.tickers:
                if t not in selected:
                    selected.append(t)
                if len(selected) >= top_n:
                    break
        return selected

    def update_5min_candle(self, symbol: str, candle: Dict, now: datetime):
        """Collect 5-min candles during 9:30-9:45 ET.

        candle: {'open': float, 'high': float, 'low': float, 'close': float, 'volume': float, 'prev_close': float}
        """
        self._reset_if_new_day(now)
        m = _minutes_from_dt(now)

        # Only collect during range window
        if not (self.RANGE_START_MIN <= m < self.RANGE_END_MIN):
            return

        r = self._ranges.setdefault(symbol, ORBRange(symbol=symbol))

        # Store prev_close and open from first candle
        if r.prev_close is None:
            r.prev_close = candle.get("prev_close")
        if r.open_price is None:
            r.open_price = candle.get("open")

        r.candles_5min.append(candle)
        if r.first_candle_volume is None:
            r.first_candle_volume = candle.get("volume")

        # Update high/low
        high = candle.get("high")
        low = candle.get("low")
        if high is not None:
            r.high = high if r.high is None else max(r.high, high)
        if low is not None:
            r.low = low if r.low is None else min(r.low, low)

        # After 3 candles (9:30-9:45 = 3x 5min), apply filters
        if len(r.candles_5min) >= 3 and not r.filtered_out:
            self._apply_filters(symbol, now)

    def _apply_filters(self, symbol: str, now: datetime):
        r = self._ranges.get(symbol)
        if not r or r.high is None or r.low is None:
            return

        # Filter 1: range 0.3%-1.5% of price
        mid_price = (r.high + r.low) / 2
        range_pct = (r.high - r.low) / mid_price if mid_price else 0
        r_range_pct = range_pct
        if not (self.RANGE_MIN_PCT <= range_pct <= self.RANGE_MAX_PCT):
            r.filtered_out = True
            r.filter_reason = f"range {range_pct*100:.2f}% not in {self.RANGE_MIN_PCT*100:.1f}-{self.RANGE_MAX_PCT*100:.1f}%"
            return

        # Filter 2: gap >3% skip
        if r.prev_close and r.open_price:
            gap_pct = abs(r.open_price - r.prev_close) / r.prev_close if r.prev_close else 0
            r.gap_pct = gap_pct
            if gap_pct > self.GAP_MAX_PCT:
                r.filtered_out = True
                r.filter_reason = f"gap {gap_pct*100:.2f}% > {self.GAP_MAX_PCT*100:.0f}%"
                return

        # Filter 3: volume first 5-min <1.5x 20d avg skip
        avg_vol = self._avg_volumes_20d.get(symbol)
        if avg_vol and r.first_candle_volume:
            ratio = r.first_candle_volume / avg_vol if avg_vol else 0
            if ratio < self.VOLUME_MIN_RATIO:
                r.filtered_out = True
                r.filter_reason = f"volume ratio {ratio:.2f} < {self.VOLUME_MIN_RATIO}"
                return

        # Filter 4: earnings day skip
        if self.is_earnings_day(symbol, now.date()):
            r.filtered_out = True
            r.filter_reason = f"earnings day for {symbol} on {now.date()}"
            return

    def get_orb_levels(self, symbol: str) -> Optional[Tuple[float, float]]:
        r = self._ranges.get(symbol)
        if not r or r.filtered_out or r.high is None or r.low is None:
            return None
        return r.high, r.low

    def check_breakout(self, symbol: str, candle_1min: Dict, now: datetime) -> Optional[ORBSignal]:
        """Monitor 1-min candles 9:45-10:30 ET for breakout.

        candle_1min: {'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}
        Returns signal if breakout confirmed.
        """
        self._reset_if_new_day(now)
        m = _minutes_from_dt(now)

        if not (self.ENTRY_START_MIN <= m < self.ENTRY_END_MIN):
            return None

        orb = self.get_orb_levels(symbol)
        if orb is None:
            return None
        orb_high, orb_low = orb

        r = self._ranges.get(symbol)
        if not r:
            return None

        close = candle_1min.get("close")
        if close is None:
            return None

        # Determine gap direction: trade only in direction of morning gap
        gap_direction = None
        if r.prev_close and r.open_price:
            if r.open_price > r.prev_close:
                gap_direction = "long"
            elif r.open_price < r.prev_close:
                gap_direction = "short"

        # Check breakout
        breakout_bias = None
        if close > orb_high:
            breakout_bias = "long"
        elif close < orb_low:
            breakout_bias = "short"
        else:
            # No breakout, clear pending
            self._pending_breakouts.pop(symbol, None)
            return None

        # Trade only gap direction
        if gap_direction and breakout_bias != gap_direction:
            return None

        # Confirmation: need close beyond range + hold next candle
        pending = self._pending_breakouts.get(symbol)
        if pending is None:
            # First breakout candle, store and wait for next candle confirmation
            self._pending_breakouts[symbol] = {
                "bias": breakout_bias,
                "breakout_close": close,
                "candle": candle_1min,
                "time": now,
            }
            return None
        else:
            # We have pending breakout, check if current candle holds beyond range
            prev_bias = pending["bias"]
            if prev_bias != breakout_bias:
                # Direction changed, reset
                self._pending_breakouts[symbol] = {
                    "bias": breakout_bias,
                    "breakout_close": close,
                    "candle": candle_1min,
                    "time": now,
                }
                return None

            # Check hold: current close still beyond ORB level
            if breakout_bias == "long" and close > orb_high:
                # Confirmed
                self._pending_breakouts.pop(symbol, None)
                return self._build_signal(symbol, breakout_bias, candle_1min, now, r, orb_high, orb_low)
            elif breakout_bias == "short" and close < orb_low:
                self._pending_breakouts.pop(symbol, None)
                return self._build_signal(symbol, breakout_bias, candle_1min, now, r, orb_high, orb_low)
            else:
                # Failed to hold, reset
                self._pending_breakouts.pop(symbol, None)
                return None

    def _build_signal(self, symbol: str, bias: str, candle_1min: Dict, now: datetime, orb_range: ORBRange, orb_high: float, orb_low: float) -> ORBSignal:
        entry = candle_1min.get("close", 0)
        # SL under low of breakout 5-min candle (for long) / above high (short)
        # We need breakout 5-min candle: use last 5-min candle in range
        breakout_5min = orb_range.candles_5min[-1] if orb_range.candles_5min else candle_1min
        if bias == "long":
            stop = breakout_5min.get("low", entry * 0.995)
            # Ensure stop below entry
            if stop >= entry:
                stop = entry * 0.995
        else:
            stop = breakout_5min.get("high", entry * 1.005)
            if stop <= entry:
                stop = entry * 1.005

        risk_dist = abs(entry - stop)
        if bias == "long":
            tp = entry + risk_dist * self.TP_R_MULT
        else:
            tp = entry - risk_dist * self.TP_R_MULT

        gap_pct = orb_range.gap_pct or 0
        range_pct = (orb_range.high - orb_range.low) / ((orb_range.high + orb_range.low) / 2) if orb_range.high and orb_range.low else 0
        vol_ratio = 0
        avg_vol = self._avg_volumes_20d.get(symbol)
        if avg_vol and orb_range.first_candle_volume:
            vol_ratio = orb_range.first_candle_volume / avg_vol if avg_vol else 0

        return ORBSignal(
            symbol=symbol,
            bias=bias,
            entry=entry,
            stop=stop,
            tp=tp,
            orb_high=orb_high,
            orb_low=orb_low,
            breakout_candle_high=breakout_5min.get("high", entry),
            breakout_candle_low=breakout_5min.get("low", entry),
            gap_pct=gap_pct,
            range_pct=range_pct,
            volume_ratio=vol_ratio,
            timestamp=now,
        )

    def should_close_all(self, now: datetime) -> bool:
        m = _minutes_from_dt(now)
        return m >= self.CLOSE_ALL_MIN

    def reset(self):
        self._ranges.clear()
        self._pending_breakouts.clear()
        self._session_date = None
