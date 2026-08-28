"""Unit tests for MT5RateLimiter (ТЗ 8.6 / P2-7)."""

from __future__ import annotations

import pytest

from mt5_adapter.errors import MT5RateLimitedError
from mt5_adapter.rate_limiter import MT5RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


def test_disabled_limiter_is_noop():
    clock = FakeClock()
    sleeps: list[float] = []
    limiter = MT5RateLimiter(None, clock=clock, sleeper=sleeps.append)
    assert not limiter.enabled
    for _ in range(100):
        assert limiter.wait() == 0.0
    assert sleeps == []


def test_rate_limiter_throttles():
    """Two calls in a row beyond the rate force a wait."""
    clock = FakeClock()
    sleeps = []

    def advancing_sleep(dt):
        sleeps.append(dt)
        clock.advance(dt)  # emulate real time.sleep

    # 1 call/sec, burst 1: first call instant, second must sleep ~1s.
    limiter = MT5RateLimiter(max_calls_per_second=1, burst=1, clock=clock, sleeper=advancing_sleep)
    limiter.wait()
    waited = limiter.wait()
    assert waited > 0.9, f"second call must be throttled, waited={waited}"
    assert sleeps, "sleeper must have been invoked"
    # The fake clock must be advanced by the sleep (like real time.sleep).
    assert clock.now > 0.0


def test_rate_limiter_allows_burst_within_limit():
    clock = FakeClock()
    sleeps: list[float] = []
    limiter = MT5RateLimiter(max_calls_per_second=10, burst=10, clock=clock, sleeper=sleeps.append)
    # Full burst without any sleep; clock not moved -> no refill needed.
    for _ in range(10):
        assert limiter.wait() == 0.0
    assert sleeps == []


def test_rate_limiter_refills_over_time():
    clock = FakeClock()
    sleeps = []

    def advancing_sleep(dt):
        sleeps.append(dt)
        clock.advance(dt)

    limiter = MT5RateLimiter(max_calls_per_second=10, burst=2, clock=clock, sleeper=advancing_sleep)
    limiter.wait()
    limiter.wait()
    # bucket empty -> next call must sleep
    limiter.wait()
    assert sleeps
    # after the sleep advanced the clock by >= 1/10s there is a new token
    clock.advance(0.5)
    assert limiter.wait() == 0.0


def test_rate_limiter_strict_mode_raises():
    limiter = MT5RateLimiter(max_calls_per_second=1, burst=1, strict=True)
    limiter.wait()
    with pytest.raises(MT5RateLimitedError):
        limiter.wait()


def test_rate_limiter_acquire_nonblocking():
    limiter = MT5RateLimiter(max_calls_per_second=1, burst=1)
    assert limiter.acquire() is True
    assert limiter.acquire() is False


def test_rate_limiter_is_thread_safe():
    import threading

    clock = FakeClock()

    def real_sleep(dt):
        clock.advance(dt)

    limiter = MT5RateLimiter(max_calls_per_second=1000, burst=1, clock=clock, sleeper=real_sleep)
    granted = []
    lock = threading.Lock()

    def worker():
        limiter.wait()
        with lock:
            granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 10
