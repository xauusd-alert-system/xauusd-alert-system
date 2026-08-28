"""Rate-based order throttling (ТЗ 8.5 + P2-10).

Responsibility (P2-10 — separation of throttle vs risk limits):
    THIS module implements ONLY frequency throttling: "no more than N orders
    per minute" per asset (and optionally globally). It deliberately knows
    NOTHING about:

    - daily trade limits        → ``risk/limits.py`` (single source);
    - daily loss / circuit breaker → ``risk/limits.py``;
    - loss-streak cooldown / risk step-down → legacy
      ``execution/trade_throttle.py`` (kept as a deprecated shim until the
      deletion phase; the engine integrates it as an optional gate).

    Guard test: ``risk/tests/test_engine.py::test_no_daily_limits_in_throttle``
    asserts ``RateThrottle`` has no daily-limit attributes (P2-10).

Inputs / outputs:
    ``RateThrottle.can_trade(asset_key)`` → ``(allowed, reason)``;
    ``RateThrottle.record_order(asset_key)`` stamps an order; a sliding
    in-memory window (no persistence — a restart legitimately re-arms rate
    control, unlike the daily budget).

Dependencies:
    stdlib only (time, threading).

Example::

    rt = RateThrottle({"risk_throttle": {"max_orders_per_minute": 6}})
    ok, reason = rt.can_trade("XAUUSD")
    if ok:
        place_order(...)
        rt.record_order("XAUUSD")
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Optional

_DEFAULT_MAX_ORDERS_PER_MINUTE = 60
_WINDOW_SECONDS = 60.0


class RateThrottle:
    """Sliding-window rate limiter: at most ``max_orders_per_minute`` orders
    per asset (P2-10: frequency only — no daily limits here)."""

    def __init__(self, cfg: Optional[dict] = None, max_orders_per_minute: Optional[int] = None):
        tc = (cfg or {}).get("risk_throttle", {}) or {}
        if max_orders_per_minute is None:
            max_orders_per_minute = int(tc.get("max_orders_per_minute", _DEFAULT_MAX_ORDERS_PER_MINUTE))
        self.max_orders_per_minute = int(max_orders_per_minute)
        self.window_seconds = float(tc.get("rate_window_seconds", _WINDOW_SECONDS))
        self._lock = threading.Lock()
        # asset_key -> deque of order timestamps (epoch seconds)
        self._orders: dict[str, deque] = defaultdict(deque)

    def can_trade(self, asset_key: str) -> tuple[bool, str]:
        """True when the asset is under the per-minute order rate."""
        with self._lock:
            window = self._orders[asset_key]
            now = time.time()
            cutoff = now - self.window_seconds
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.max_orders_per_minute:
                oldest = window[0]
                wait = int(oldest + self.window_seconds - now) + 1
                return False, (
                    f"rate_throttled: {len(window)}/"
                    f"{self.max_orders_per_minute} orders in the last "
                    f"{int(self.window_seconds)}s for {asset_key}; "
                    f"wait {wait}s"
                )
            return True, "OK"

    def record_order(self, asset_key: str) -> None:
        """Stamp an order into the sliding window."""
        with self._lock:
            self._orders[asset_key].append(time.time())

    def clear(self, asset_key: Optional[str] = None) -> None:
        """Reset the window (tests / manual override)."""
        with self._lock:
            if asset_key is None:
                self._orders.clear()
            else:
                self._orders.pop(asset_key, None)
