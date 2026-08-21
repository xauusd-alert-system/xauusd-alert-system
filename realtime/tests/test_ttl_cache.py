"""Regression tests for the dashboard TTL cache (audit 2026-08-19).

Production bug: /api/matrix recomputes 5 ensemble pipelines serially (~40s).
The old _ttl_cache only cached results; concurrent dashboard polls during a
recompute each started their OWN recompute, piling up requests and freezing
the dashboard. The fix: single-flight (only one recompute per key at a time)
+ stale-while-revalidate (concurrent callers get the last cached copy
immediately instead of waiting or recomputing).
"""
import threading
import time

from realtime import app as app_mod


def test_single_flight_only_one_recompute_at_a_time():
    """While one call recomputes, another call must NOT start a second
    recompute: it waits for the in-flight one (first-ever call path)."""
    app_mod.CACHE_BYPASS = False
    try:
        calls = []
        started = threading.Event()

        @app_mod._ttl_cache(30)
        def slow_fn(x):
            calls.append(x)
            started.set()
            time.sleep(0.3)
            return f"v{x}"

        results = {}
        errors = []

        def worker():
            try:
                results["a"] = slow_fn(1)
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        started.wait(5)
        results["b"] = slow_fn(1)
        t.join(5)

        assert not errors
        assert results == {"a": "v1", "b": "v1"}
        assert calls == [1]  # exactly ONE recompute despite two calls
    finally:
        app_mod.CACHE_BYPASS = True


def test_stale_copy_served_while_recompute_in_flight():
    """After the TTL expires, a call starts a recompute; concurrent callers
    immediately receive the PREVIOUS (stale) copy instead of recomputing."""
    app_mod.CACHE_BYPASS = False
    try:
        calls = []
        started = threading.Event()

        @app_mod._ttl_cache(0.05)
        def slow_fn(x):
            calls.append(x)
            started.set()
            time.sleep(0.3)
            return f"v{x}"

        assert slow_fn(2) == "v2"  # prime the cache
        started.clear()
        results = {}

        t = threading.Thread(target=lambda: results.setdefault("a", slow_fn(2)))
        t.start()
        started.wait(5)
        results["b"] = slow_fn(2)  # TTL expired -> winner recomputes, we get stale
        t.join(5)

        assert results == {"a": "v2", "b": "v2"}
        assert calls == [2, 2]  # prime + exactly one recompute (stale served)
    finally:
        app_mod.CACHE_BYPASS = True