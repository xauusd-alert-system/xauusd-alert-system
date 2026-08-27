"""Deep coverage tests for shared infrastructure components (Phase 4)."""
import json
import logging
import sqlite3
import time
from unittest.mock import MagicMock
import pytest

from shared.cache import TTLCache, cached
from shared.container import BotContainer, build_container
from shared.db import SQLiteConnectionPool
from shared.logging import JSONLogFormatter, get_structured_logger
from shared.metrics import MetricsRegistry
from shared.retry import retry, retry_with_backoff


def test_json_log_formatter():
    logger = get_structured_logger("test_structured_logger")
    formatter = JSONLogFormatter()
    
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=42,
        msg="Test event message",
        args=(),
        exc_info=None,
    )
    record.symbol = "AAPL"
    record.signal_id = "sig_123"
    
    out = formatter.format(record)
    parsed = json.loads(out)
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "Test event message"
    assert parsed["symbol"] == "AAPL"
    assert parsed["signal_id"] == "sig_123"
    assert "timestamp" in parsed

    # Exception formatting
    try:
        raise ValueError("Simulated error")
    except ValueError:
        import sys
        record_exc = logging.LogRecord(
            name="test_logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=50,
            msg="Error occurred",
            args=(),
            exc_info=sys.exc_info(),
        )
        out_exc = formatter.format(record_exc)
        parsed_exc = json.loads(out_exc)
        assert "exception" in parsed_exc
        assert "Simulated error" in parsed_exc["exception"]


def test_ttl_cache_extended():
    cache = TTLCache(default_ttl_seconds=0.1)
    cache.set("a", 100)
    assert "a" in cache
    assert cache.get("a") == 100
    assert len(cache) == 1

    cache.set("b", 200, ttl=0.05)
    assert cache.get("b") == 200

    time.sleep(0.06)
    assert cache.get("b") is None
    assert "b" not in cache

    cache.delete("a")
    assert cache.get("a") is None

    # Cached decorator test
    calls = 0
    @cached(cache, ttl_seconds=1.0)
    def compute(x: int) -> int:
        nonlocal calls
        calls += 1
        return x * 2

    assert compute(5) == 10
    assert calls == 1
    assert compute(5) == 10
    assert calls == 1
    assert compute(6) == 12
    assert calls == 2

    cache.clear()
    assert len(cache) == 0


def test_retry_decorator_and_predicate():
    attempts = 0

    def fail_twice(x: int) -> int:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("Network glitch")
        return x * 10

    res = retry_with_backoff(
        fail_twice,
        5,
        max_retries=3,
        initial_delay=0.01,
        backoff_factor=1.5,
        jitter=False,
        retry_exceptions=(ConnectionError,),
        sleep_fn=lambda _: None,
    )
    assert res == 50
    assert attempts == 3

    # Retry predicate test
    def non_retriable_condition(e: Exception) -> bool:
        return "fatal" not in str(e)

    def fatal_func():
        raise RuntimeError("fatal error")

    with pytest.raises(RuntimeError, match="fatal error"):
        retry_with_backoff(
            fatal_func,
            max_retries=3,
            initial_delay=0.01,
            retry_predicate=non_retriable_condition,
            sleep_fn=lambda _: None,
        )

    # Decorator test
    dec_attempts = 0
    @retry(max_retries=2, initial_delay=0.01, retry_exceptions=(ValueError,), jitter=False)
    def decorated_func():
        nonlocal dec_attempts
        dec_attempts += 1
        if dec_attempts < 2:
            raise ValueError("First attempt fails")
        return "success"

    assert decorated_func() == "success"
    assert dec_attempts == 2


def test_sqlite_connection_pool_busy_and_close(tmp_path):
    db_file = tmp_path / "test_pool.sqlite"
    pool = SQLiteConnectionPool(str(db_file), timeout=2.0)
    
    with pool.get_connection() as conn1:
        conn1.execute("CREATE TABLE items (id INT, val TEXT)")
        conn1.execute("INSERT INTO items VALUES (1, 'one')")
        conn1.commit()

    # Verify query works across connections
    with pool.get_connection() as conn2:
        row = conn2.execute("SELECT val FROM items WHERE id=1").fetchone()
        assert row["val"] == "one"

    pool.close_all()
