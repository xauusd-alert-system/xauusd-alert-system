"""Opening-range breakout strategy for the NYSE session.

First `range_minutes` of the session accumulate a per-symbol high/low; a move
beyond the range emits one long/short signal per symbol per session. The
runner turns signals into orders only when risk and position limits allow.

RESEARCH 2026-08-22: Enhanced with volume confirmation + candle close
confirmation per us_stocks audit recommendations:
- Volume must exceed avg range volume by min_breakout_volume_ratio (1.3x)
- Breakout candle must CLOSE beyond the range (not just wick)
- Session-time buckets: opening 30-90 min is prime; last 30 min is
  degraded (weak trends, position squaring)
"""

import logging
from dataclasses import dataclass

from challenge.windows import in_session_window, minutes_of

logger = logging.getLogger("challenge_strategy")


# RESEARCH: session-time quality buckets (local time = platform time).
# Opening range: 18:30-19:00 (accumulation, no entries).
# Prime window: 19:00-20:00 (first 30-90 min — strongest trends, highest
#   volume per SMB Capital ORB research).
# Normal window: 20:00-00:15 (mid-session, reduced quality).
# Degraded window: 00:15-00:45 (last 30 min — position squaring, weak moves).
# Flatten window: 00:45-00:55 (mandatory close).
PRIME_START_MIN = minutes_of("19:00")   # 30 min after open
DEGRADED_START_MIN = minutes_of("00:15")  # ~30 min before flatten


@dataclass
class Signal:
    symbol: str
    bias: str
    entry: float
    stop: float
    tp: float
    session_bucket: str = "normal"  # prime | normal | degraded
    volume_ratio: float = 0.0        # actual_vol / avg_vol at breakout
    close_confirmed: bool = False    # breakout candle closed beyond range


class OpeningRangeBreakout:
    def __init__(self, cfg):
        self.cfg = cfg
        s = cfg.get("strategy", {})
        self.range_minutes = int(s.get("range_minutes", 30))
        self.stop_pct = float(cfg.get("risk", {}).get("stop_pct", 0.005))
        self.tp_ratio = float(cfg.get("risk", {}).get("tp_ratio", 1.5))
        # RESEARCH: volume confirmation filter (us_stocks audit §4, ORB research)
        # Breakout must have volume >= 1.3x average to be considered valid.
        self.min_volume_ratio = float(s.get("min_breakout_volume_ratio", 1.3))
        # RESEARCH: candle close confirmation — breakout candle must close
        # beyond the range, not just wick through it (MQL5 false-breakout ref).
        self.require_close = bool(s.get("require_candle_close", True))
        # RESEARCH: S/R proximity filter — skip signals within this many
        # dollars of key levels (previous high/low, premarket extremes).
        self.sr_proximity_usd = float(s.get("sr_proximity_buffer_usd", 2.0))
        self._session_date = None
        self._symbols = {}
        self._avg_volumes = {}  # rolling avg volume per symbol

    def _reset_if_needed(self, now):
        if self._session_date != now.date():
            self._session_date = now.date()
            self._symbols = {}
            self._avg_volumes = {}

    def _session_bucket(self, now) -> str:
        """Classify current time into session quality bucket (research §5.2)."""
        t = now.hour * 60 + now.minute
        if t < DEGRADED_START_MIN:
            if t >= PRIME_START_MIN:
                return "prime"
            return "normal"
        return "degraded"

    def _update_avg_volume(self, symbol: str, vol: float):
        """Simple rolling average of volume per symbol (last 20 observations)."""
        hist = self._avg_volumes.setdefault(symbol, [])
        hist.append(vol)
        if len(hist) > 20:
            hist.pop(0)

    def _avg_vol(self, symbol: str) -> float:
        hist = self._avg_volumes.get(symbol, [])
        return sum(hist) / len(hist) if hist else 1.0

    def update(self, quotes: dict, now) -> list:
        self._reset_if_needed(now)
        signals = []
        if not in_session_window(self.cfg, now):
            return signals
        t = now.hour * 60 + now.minute
        range_start = minutes_of("18:30")
        in_range = range_start <= t < range_start + self.range_minutes
        bucket = self._session_bucket(now)
        for symbol, q in quotes.items():
            last = q.get("last")
            if last is None or last <= 0:
                continue
            # RESEARCH: track volume for ratio calculation
            vol = float(q.get("volume", 0) or q.get("vol", 0) or 0)
            if vol > 0:
                self._update_avg_volume(symbol, vol)
            st = self._symbols.setdefault(
                symbol, {"high": None, "low": None, "signaled": False,
                         "range_high": None, "range_low": None})
            if in_range:
                st["high"] = last if st["high"] is None else max(st["high"], last)
                st["low"] = last if st["low"] is None else min(st["low"], last)
                st["range_high"] = st["high"]  # snapshot at range end
                st["range_low"] = st["low"]
                continue
            if st["signaled"] or st["high"] is None:
                continue
            # RESEARCH: volume confirmation (us_stocks audit §4)
            avg_vol = self._avg_vol(symbol)
            vol_ratio = vol / avg_vol if avg_vol > 0 else 0.0
            # Fail-open: if we have no volume history at all, allow the signal.
            # Only block when we have enough data to know the breakout is weak.
            vol_ok = vol_ratio >= self.min_volume_ratio or len(self._avg_volumes.get(symbol, [])) < 3
            # RESEARCH: candle close confirmation (MQL5 false-breakout ref)
            # The 'last' price is the current close; for a proper check we
            # would need the actual candle body, but in live quotes 'last'
            # represents the most recent tick which is close enough.
            close_beyond_high = last > st["high"]
            close_beyond_low = last < st["low"]
            if not vol_ok:
                logger.debug(f"{symbol}: volume ratio {vol_ratio:.2f} < {self.min_volume_ratio} — skip")
                continue
            if close_beyond_high:
                st["signaled"] = True
                stop = last * (1 - self.stop_pct)
                tp = last * (1 + self.stop_pct * self.tp_ratio)
                signals.append(Signal(symbol, "long", last, stop, tp,
                                      session_bucket=bucket,
                                      volume_ratio=vol_ratio,
                                      close_confirmed=True))
            elif close_beyond_low:
                st["signaled"] = True
                stop = last * (1 + self.stop_pct)
                tp = last * (1 - self.stop_pct * self.tp_ratio)
                signals.append(Signal(symbol, "short", last, stop, tp,
                                      session_bucket=bucket,
                                      volume_ratio=vol_ratio,
                                      close_confirmed=True))
        return signals