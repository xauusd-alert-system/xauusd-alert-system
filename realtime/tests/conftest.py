"""
Shared fixtures for realtime app tests.

The dashboard endpoints (/signal, /api/matrix, /api/monte-carlo) carry a
process-local TTL cache that serves dashboard polling without re-computing
serially. Tests monkeypatch pipelines per-test, so the cache must be off
for the whole test session.
"""

import pytest

import realtime.app as app_mod


@pytest.fixture(autouse=True)
def _bypass_realtime_app_cache():
    app_mod.CACHE_BYPASS = True
    yield
    app_mod.CACHE_BYPASS = False
