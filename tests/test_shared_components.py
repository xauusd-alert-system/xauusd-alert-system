"""Tests for shared package components (P2-1, P2-2, P2-6, P2-7, P2-8, P2-9, P2-10, P2-11, P2-12)."""
import os
import sqlite3
import tempfile
import time
from unittest.mock import MagicMock
import pytest

from shared.cache import TTLCache
from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, CircuitState
from shared.container import build_container
from shared.db import SQLiteConnectionPool, configure_sqlite_connection
from shared.metrics import MetricsRegistry
from shared.retry import retry, retry_with_backoff
from shared.risk_protocol import RiskDecision, RiskEngineProtocol


def test_circuit_breaker_transitions():
    clock_time = 1000.0
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0, clock=lambda: clock_time)

    assert cb.state == CircuitState.CLOSED
    assert cb.is_available

    # First failure
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED

    # Second failure -> transitions to OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert not cb.is_available

    # Calling while OPEN raises CircuitBreakerOpenError
    with pytest.raises(CircuitBreakerOpenError):
        cb.call(lambda: "should not run")

    # Fast-forward past recovery timeout -> transitions to HALF_OPEN
    clock_time += 31.0
    assert cb.is_available
    assert cb.state == CircuitState.HALF_OPEN

    # Success in HALF_OPEN resets to CLOSED
    res = cb.call(lambda: "success_result")
    assert res == "success_result"
    assert cb.state == CircuitState.CLOSED


def test_retry_with_backoff_success():
    attempts = 0

    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("Temporary network blip")
        return "ok"

    res = retry_with_backoff(
        flaky, max_retries=3, initial_delay=0.01, retry_exceptions=(ConnectionError,), sleep_fn=lambda _: None
    )
    assert res == "ok"
    assert attempts == 3


def test_retry_exhausted_raises():
    def always_fails():
        raise ValueError("Permanent failure")

    with pytest.raises(ValueError):
        retry_with_backoff(
            always_fails, max_retries=2, initial_delay=0.01, sleep_fn=lambda _: None
        )


def test_ttl_cache_expiration():
    clock_time = 1000.0
    cache = TTLCache(default_ttl_seconds=10.0, clock=lambda: clock_time)

    cache.set("foo", "bar")
    assert cache.get("foo") == "bar"
    assert "foo" in cache

    clock_time += 11.0
    assert cache.get("foo") is None
    assert "foo" not in cache


def test_sqlite_connection_pool_and_pragmas():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_pool.sqlite")
        pool = SQLiteConnectionPool(db_path)

        with pool.cursor() as cur:
            cur.execute("CREATE TABLE items (id INT, val TEXT)")
            cur.execute("INSERT INTO items VALUES (1, 'hello')")

        conn = pool.get_connection()
        row = conn.execute("SELECT * FROM items").fetchone()
        assert row["val"] == "hello"

        # Check WAL mode is enabled
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.upper() == "WAL"

        pool.close_all()


def test_metrics_registry_prometheus_rendering():
    reg = MetricsRegistry()
    c = reg.counter("signals_total", "Total signals generated")
    g = reg.gauge("active_positions", "Current active positions count")

    c.inc(1, symbol="AAPL")
    c.inc(2, symbol="AAPL")
    g.set(1.0)

    assert c.get(symbol="AAPL") == 3.0
    assert g.get() == 1.0

    rendered = reg.render_prometheus()
    assert "signals_total" in rendered
    assert 'symbol="AAPL"' in rendered
    assert "active_positions" in rendered


def test_container_build():
    with tempfile.TemporaryDirectory() as td:
        cfg_path = os.path.join(td, "cfg.yaml")
        with open(cfg_path, "w") as f:
            f.write("risk:\n  max_trades_per_day: 5\nchallenge:\n  max_notional_usd: 5000\n")

        container = build_container(cfg_path, custom_provider=MagicMock())
        assert container.risk_engine.max_trades_per_day == 5
        assert container.runner is not None
        assert container.journal is not None
        container.journal.close()
