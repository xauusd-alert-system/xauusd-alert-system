"""ORBStrategy — Opening Range Breakout for US equities (TSLA, AAPL, etc.).

Collects 3× 5-min candles during 9:30-9:45 ET to define the opening range,
then monitors 1-min candles until 10:30 ET for breakout entries.

Ticker rotation is by premarket volume, not fixed per day.

This module contains ONLY strategy logic (filters, range, breakout).
Execution is handled by ``StealthExecutionEngine``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("orb_strategy")


@dataclass
class ORBSignal:
    """A breakout signal from the ORB strategy."""
    symbol: str
    bias: str                  # "long" | "short"
    entry: float
    stop: float
    tp: float
    range_high: float
    range_low: float
    range_pct: float           # (high - low) / mid * 100
    volume_ratio: float        # first-candle vol / 20d avg
    gap_pct: float             # opening gap % from prev close
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class TickerState:
    """Per-ticker accumulation state for a single trading day."""
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    range_candles: int = 0       # how many 5-min candles collected
    signaled: bool = False
    first_candle_volume: float = 0.0
    prev_close: Optional[float] = None
    open_price: Optional[float] = None


class ORBStrategy:
    """Opening Range Breakout strategy for US equities.

    Parameters
    ----------
    cfg : dict
        Config dict with strategy, risk sections.
    seed : int | None
        Optional RNG seed (for future randomization of filters).
    """

    # --- Defaults ---
    DEFAULT_TICKERS = ["TSLA", "AAPL", "NVDA", "AMZN", "META"]
    RANGE_MINUTES: int = 15        # 9:30-9:45 = 15 min of 5-min candles
    ENTRY_END: str = "10:30"
    ALL_CLOSE: str = "15:30"
    MIN_RANGE_PCT: float = 0.3
    MAX_RANGE_PCT: float = 1.5
    GAP_SKIP_PCT: float = 3.0     # skip if opening gap > 3%
    MIN_VOLUME_RATIO: float = 1.5 # first 5-min vol < 1.5× 20d avg → skip
    TP_R: float = 2.0             # TP at 2R
    RISK_USD: float = 10.0        # $10 base risk

    def __init__(self, cfg: dict, *, seed: int | None = None) -> None:
        c = cfg.get("strategy", {})
        self._cfg = cfg
        self._seed = seed

        self.tickers: List[str] = c.get("tickers", self.DEFAULT_TICKERS)
        self.range_minutes = int(c.get("range_minutes", self.RANGE_MINUTES))
        self.range_start_hm = c.get("orb_range_start", "09:30")
        self.range_end_hm = c.get("orb_range_end", "09:45")
        self.entry_end = c.get("entry_end", self.ENTRY_END)
        self.all_close = c.get("all_positions_close", self.ALL_CLOSE)
        self.min_range_pct = float(c.get("min_range_pct", self.MIN_RANGE_PCT))
        self.max_range_pct = float(c.get("max_range_pct", self.MAX_RANGE_PCT))
        self.gap_skip_pct = float(c.get("gap_skip_pct", self.GAP_SKIP_PCT))
        self.min_volume_ratio = float(c.get("min_volume_ratio", self.MIN_VOLUME_RATIO))
        self.tp_r = float(c.get("tp_r", self.TP_R))

        # State per day
        self._state_date: Optional[date] = None
        self._tickers: Dict[str, TickerState] = {}
        self._avg_volumes: Dict[str, List[float]] = {}

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def et_offset_hours(d: date) -> int:
        """US Eastern offset for a given date: -4 (EDT) in DST, -5 (EST) otherwise.

        DST runs from the 2nd Sunday of March (02:00 local) to the 1st Sunday
        of November (02:00 local).  Use this instead of a fixed -4 constant so
        session windows don't drift by an hour in winter.
        """
        first_mar = date(d.year, 3, 1)
        first_sun_mar = first_mar + timedelta(days=(6 - first_mar.weekday()) % 7)
        dst_start = first_sun_mar + timedelta(days=7)
        first_nov = date(d.year, 11, 1)
        dst_end = first_nov + timedelta(days=(6 - first_nov.weekday()) % 7)
        return -4 if dst_start <= d < dst_end else -5

    @staticmethod
    def _hm_to_minutes(hm: str) -> int:
        h, m = map(int, hm.split(":"))
        return h * 60 + m

    def _now_minutes(self, now_et: datetime) -> int:
        return now_et.hour * 60 + now_et.minute

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _reset_if_needed(self, now_et: datetime) -> None:
        if self._state_date != now_et.date():
            self._state_date = now_et.date()
            self._tickers = {}

    def _get_state(self, symbol: str) -> TickerState:
        return self._tickers.setdefault(symbol, TickerState())

    def _update_avg_volume(self, symbol: str, vol: float) -> float:
        """Rolling 20-day average volume. Returns current average."""
        hist = self._avg_volumes.setdefault(symbol, [])
        hist.append(vol)
        if len(hist) > 20:
            hist.pop(0)
        return sum(hist) / len(hist) if hist else 1.0

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------

    def _check_range_pct(self, high: float, low: float) -> bool:
        """Range must be 0.3%-1.5% of mid price."""
        if high <= 0 or low <= 0:
            return False
        mid = (high + low) / 2
        rng_pct = abs(high - low) / mid * 100
        return self.min_range_pct <= rng_pct <= self.max_range_pct

    def _check_gap(self, open_price: float, prev_close: float) -> bool:
        """Skip if opening gap > 3%."""
        if prev_close <= 0:
            return True  # no data → allow
        gap = abs(open_price - prev_close) / prev_close * 100
        return gap <= self.gap_skip_pct

    def _check_volume(self, first_vol: float, avg_vol: float) -> bool:
        """First 5-min volume must be ≥ 1.5× 20d average."""
        if avg_vol <= 0:
            return True  # no data → allow
        return first_vol >= self.min_volume_ratio * avg_vol

    def _check_gap_direction(self, bias: str, open_price: float, prev_close: float) -> bool:
        """Trade ONLY in the direction of the morning gap."""
        if prev_close <= 0:
            return True  # no prev close → allow
        gap_up = open_price > prev_close
        gap_down = open_price < prev_close
        if not gap_up and not gap_down:
            return True  # flat → allow both
        if bias == "long" and gap_up:
            return True
        if bias == "short" and gap_down:
            return True
        return False

    # ------------------------------------------------------------------
    # Main update loop
    # ------------------------------------------------------------------

    def update(
        self,
        candles_5min: Dict[str, Dict[str, Any]],
        candles_1min: Dict[str, Dict[str, Any]],
        now_et: datetime,
    ) -> List[ORBSignal]:
        """Feed candle data and return any new breakout signals.

        Parameters
        ----------
        candles_5min : dict
            ``{symbol: {"high": float, "low": float, "open": float,
             "close": float, "volume": float, "prev_close": float}}``
        candles_1min : dict
            ``{symbol: {"high": float, "low": float, "close": float,
             "volume": float}}``
        now_et : datetime
            Current time in Eastern Time (naive or tz-aware).
        """
        self._reset_if_needed(now_et)
        signals: List[ORBSignal] = []
        t = self._now_minutes(now_et)
        range_start = self._hm_to_minutes(self.range_start_hm)
        range_end = self._hm_to_minutes(self.range_end_hm)
        entry_end = self._hm_to_minutes(self.entry_end)

        for symbol in self.tickers:
            st = self._get_state(symbol)

            # --- Range phase: collect 3× 5-min candles (9:30-9:45 ET) ---
            if range_start <= t < range_end:
                c5 = candles_5min.get(symbol)
                if c5:
                    h = c5.get("high")
                    l = c5.get("low")
                    vol = float(c5.get("volume", 0) or 0)
                    if h is not None and l is not None:
                        st.range_high = h if st.range_high is None else max(st.range_high, h)
                        st.range_low = l if st.range_low is None else min(st.range_low, l)
                        st.range_candles += 1
                        if st.range_candles == 1:
                            st.first_candle_volume = vol
                            st.open_price = c5.get("open")
                            st.prev_close = c5.get("prev_close")
                    # Update avg volume
                    avg = self._update_avg_volume(symbol, vol)
                continue  # No entries during range phase

            # --- Entry phase: 9:45-10:30 ET ---
            if range_end <= t < entry_end and not st.signaled:
                if st.range_high is None or st.range_low is None:
                    continue

                c1 = candles_1min.get(symbol)
                if c1 is None:
                    continue

                close = c1.get("close")
                if close is None:
                    continue

                # Update avg volume
                vol = float(c1.get("volume", 0) or 0)
                avg = self._update_avg_volume(symbol, vol)
                if st.first_candle_volume == 0:
                    st.first_candle_volume = vol

                # --- Apply filters ---
                # 1. Range percentage filter
                if not self._check_range_pct(st.range_high, st.range_low):
                    logger.debug(
                        "%s: range %.2f-%.2f outside %.1f%%-%.1f%% filter",
                        symbol, st.range_low, st.range_high,
                        self.min_range_pct, self.max_range_pct,
                    )
                    continue

                # 2. Gap filter
                if st.open_price and st.prev_close:
                    if not self._check_gap(st.open_price, st.prev_close):
                        logger.debug("%s: gap too large, skipping", symbol)
                        continue

                # 3. Volume filter
                vol_ratio = st.first_candle_volume / avg if avg > 0 else 0
                if not self._check_volume(st.first_candle_volume, avg):
                    logger.debug(
                        "%s: vol ratio %.2f < %.1f, skipping",
                        symbol, vol_ratio, self.min_volume_ratio,
                    )
                    continue

                # --- Breakout detection ---
                breakout_high = close > st.range_high
                breakout_low = close < st.range_low

                if breakout_high:
                    bias = "long"
                elif breakout_low:
                    bias = "short"
                else:
                    continue  # no breakout

                # 4. Gap direction filter
                if st.open_price and st.prev_close:
                    if not self._check_gap_direction(bias, st.open_price, st.prev_close):
                        logger.debug(
                            "%s: gap direction mismatch (%s vs gap), skipping",
                            symbol, bias,
                        )
                        continue

                # 5. Confirmation: next candle holds beyond range
                # (We use the current close as confirmation since we're
                # on 1-min data)
                if bias == "long" and close <= st.range_high:
                    continue
                if bias == "short" and close >= st.range_low:
                    continue

                # --- Compute entry, SL, TP ---
                entry = close
                risk_dist = abs(close - (st.range_low if bias == "long" else st.range_high))
                if risk_dist <= 0:
                    risk_dist = abs(st.range_high - st.range_low) / 2

                if bias == "long":
                    stop = st.range_low  # SL under the 5-min breakout candle low
                    tp = entry + risk_dist * self.tp_r
                else:
                    stop = st.range_high  # SL above the 5-min breakout candle high
                    tp = entry - risk_dist * self.tp_r

                # Range %
                mid = (st.range_high + st.range_low) / 2
                range_pct = abs(st.range_high - st.range_low) / mid * 100 if mid > 0 else 0

                # Gap %
                gap_pct = 0.0
                if st.prev_close and st.prev_close > 0:
                    gap_pct = (st.open_price - st.prev_close) / st.prev_close * 100

                st.signaled = True
                signals.append(ORBSignal(
                    symbol=symbol,
                    bias=bias,
                    entry=entry,
                    stop=stop,
                    tp=tp,
                    range_high=st.range_high,
                    range_low=st.range_low,
                    range_pct=range_pct,
                    volume_ratio=vol_ratio,
                    gap_pct=gap_pct,
                    timestamp=now_et,
                ))
                logger.info(
                    "ORB breakout: %s %s @ %.2f (SL %.2f / TP %.2f, "
                    "range %.2f%%, vol %.1fx, gap %.1f%%)",
                    bias.upper(), symbol, entry, stop, tp,
                    range_pct, vol_ratio, gap_pct,
                )

        return signals

    # ------------------------------------------------------------------
    # Premarket rotation
    # ------------------------------------------------------------------

    def rank_by_premarket_volume(
        self,
        premarket_volumes: Dict[str, float],
    ) -> List[str]:
        """Rank tickers by premarket volume (highest first).

        Called before market open to decide which tickers to watch.
        """
        ranked = sorted(
            self.tickers,
            key=lambda t: premarket_volumes.get(t, 0),
            reverse=True,
        )
        return ranked
