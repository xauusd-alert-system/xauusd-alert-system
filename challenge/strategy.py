"""Opening-range breakout strategy for the NYSE session.

First `range_minutes` of the session accumulate a per-symbol high/low; a move
beyond the range emits one long/short signal per symbol per session. The
runner turns signals into orders only when risk and position limits allow.
"""

from dataclasses import dataclass

from challenge.windows import in_session_window, minutes_of


@dataclass
class Signal:
    symbol: str
    bias: str
    entry: float
    stop: float
    tp: float


class OpeningRangeBreakout:
    def __init__(self, cfg):
        self.cfg = cfg
        s = cfg.get("strategy", {})
        self.range_minutes = int(s.get("range_minutes", 30))
        self.stop_pct = float(cfg.get("risk", {}).get("stop_pct", 0.005))
        self.tp_ratio = float(cfg.get("risk", {}).get("tp_ratio", 1.5))
        self._session_date = None
        self._symbols = {}

    def _reset_if_needed(self, now):
        if self._session_date != now.date():
            self._session_date = now.date()
            self._symbols = {}

    def update(self, quotes: dict, now) -> list:
        self._reset_if_needed(now)
        signals = []
        if not in_session_window(self.cfg, now):
            return signals
        t = now.hour * 60 + now.minute
        range_start = minutes_of("18:30")
        in_range = range_start <= t < range_start + self.range_minutes
        for symbol, q in quotes.items():
            last = q.get("last")
            if last is None or last <= 0:
                continue
            st = self._symbols.setdefault(
                symbol, {"high": None, "low": None, "signaled": False})
            if in_range:
                st["high"] = last if st["high"] is None else max(st["high"], last)
                st["low"] = last if st["low"] is None else min(st["low"], last)
                continue
            if st["signaled"] or st["high"] is None:
                continue
            if last > st["high"]:
                st["signaled"] = True
                stop = last * (1 - self.stop_pct)
                tp = last * (1 + self.stop_pct * self.tp_ratio)
                signals.append(Signal(symbol, "long", last, stop, tp))
            elif last < st["low"]:
                st["signaled"] = True
                stop = last * (1 + self.stop_pct)
                tp = last * (1 - self.stop_pct * self.tp_ratio)
                signals.append(Signal(symbol, "short", last, stop, tp))
        return signals