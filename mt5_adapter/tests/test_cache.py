"""Unit tests for SymbolCache (ТЗ 8.6)."""

from __future__ import annotations

from mt5_adapter.cache import SymbolCache


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_cache_returns_within_ttl():
    clock = FakeClock()
    cache = SymbolCache(ttl_ms=500, clock=clock)
    fetches = []

    def fetcher():
        fetches.append(1)
        return {"bid": 2400.0}

    assert cache.get_or_fetch("XAUUSD", fetcher) == {"bid": 2400.0}
    clock.now = 0.4  # 400ms < 500ms TTL
    assert cache.get_or_fetch("XAUUSD", fetcher) == {"bid": 2400.0}
    assert len(fetches) == 1
    assert cache.hits == 1
    assert cache.misses == 1


def test_cache_refetches_after_ttl():
    clock = FakeClock()
    cache = SymbolCache(ttl_ms=500, clock=clock)
    calls: list[int] = []

    def fetcher():
        calls.append(len(calls))
        return {"n": len(calls)}

    first = cache.get_or_fetch("XAUUSD", fetcher)
    clock.now = 0.6  # 600ms > 500ms TTL
    second = cache.get_or_fetch("XAUUSD", fetcher)
    assert first == {"n": 1}
    assert second == {"n": 2}
    assert len(calls) == 2


def test_cache_none_result_not_cached():
    cache = SymbolCache(ttl_ms=1000)
    calls = []

    def flaky():
        calls.append(1)
        return None if len(calls) == 1 else "fresh"

    assert cache.get_or_fetch("XAUUSD", flaky) is None
    assert cache.get_or_fetch("XAUUSD", flaky) == "fresh"
    assert len(calls) == 2


def test_cache_invalidate():
    clock = FakeClock()
    cache = SymbolCache(ttl_ms=10_000, clock=clock)
    cache.set("XAUUSD", 1)
    assert cache.get("XAUUSD") == (True, 1)
    cache.invalidate("XAUUSD")
    assert cache.get("XAUUSD") == (False, None)
    cache.set("XAUUSD", 2)
    cache.invalidate()
    assert cache.size() == 0


def test_cache_keys_are_independent():
    cache = SymbolCache(ttl_ms=10_000)
    cache.set(("tick", "XAUUSD"), 1)
    cache.set(("tick", "BTCUSD"), 2)
    assert cache.get(("tick", "XAUUSD")) == (True, 1)
    assert cache.get(("tick", "BTCUSD")) == (True, 2)
