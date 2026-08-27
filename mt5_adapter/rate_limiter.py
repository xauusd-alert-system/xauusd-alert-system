"""Thread-safe rate limiter for MetaTrader5 calls (ТЗ 8.6 / P2-7).

MT5's Python API is a single shared terminal connection; flooding it with
rapid-fire calls (e.g. per-tick polling across many symbols) can starve the
terminal's own queue and distort tick timestamps. The limiter implements a
token-bucket with a configurable ``max_calls_per_second`` (default 10).

The limiter is DISABLED by default (``max_calls_per_second=None``) so existing
test/paper behaviour does not change; the mechanism is opt-in via config
(``mt5_adapter.rate_limit.max_calls_per_second`` or the
``MT5_RATE_LIMIT_MAX_CPS`` env var).
"""
from __future__ import annotations

import threading
import time


class MT5RateLimiter:
    """Token-bucket rate limiter, thread-safe via ``threading.Lock``.

    Args:
        max_calls_per_second: sustained call rate. ``None`` (default) or
            ``<= 0`` disables throttling entirely (``wait()`` is a no-op).
        burst: bucket capacity (maximum burst within one second). Defaults to
            ``max(1, max_calls_per_second)``.
        clock: monotonic time provider (injectable for tests).
        sleeper: sleep function (injectable for tests).
        strict: when True, ``wait()`` raises :class:`~mt5_adapter.errors.
            MT5RateLimitedError` instead of sleeping.
    """

    def __init__(
        self,
        max_calls_per_second: int | float | None = None,
        burst: int | float | None = None,
        clock=time.monotonic,
        sleeper=time.sleep,
        strict: bool = False,
    ):
        self.clock = clock
        self.sleeper = sleeper
        self.strict = bool(strict)
        if max_calls_per_second is None or max_calls_per_second <= 0:
            self.rate: float | None = None
            self.burst: float = 0.0
        else:
            self.rate = float(max_calls_per_second)
            self.burst = float(burst) if burst else max(1.0, self.rate)
        self._tokens = self.burst
        self._last_refill = self.clock()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.rate is not None

    def _refill(self, now: float) -> None:
        if self.rate is None:
            return
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now

    def wait(self) -> float:
        """Block until a call slot is available. Returns seconds waited.

        With ``strict=True`` raises ``MT5RateLimitedError`` instead of
        sleeping when no slot is immediately available."""
        if not self.enabled:
            return 0.0

        waited = 0.0
        while True:
            with self._lock:
                now = self.clock()
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                deficit = 1.0 - self._tokens
                need = deficit / self.rate if self.rate else 0.0

            if self.strict:
                from mt5_adapter.errors import MT5RateLimitedError
                raise MT5RateLimitedError(
                    f"MT5 rate limit exceeded ({self.rate}/s, strict mode)")

            chunk = max(0.001, min(need, 0.25))
            self.sleeper(chunk)
            waited += chunk

    def acquire(self) -> bool:
        """Non-blocking variant: True when a slot was consumed immediately."""
        if not self.enabled:
            return True
        with self._lock:
            now = self.clock()
            self._refill(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False
