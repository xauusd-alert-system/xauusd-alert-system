"""Security audit tests: SQL injection, Telegram replay protection, input sanitization, and fail-closed guards (Phase 4 / Phase 8)."""
import os
import tempfile
from datetime import datetime, timezone
import pytest

from execution.disabled_executor import DisabledExecutor
from usstocks.guards import assert_auto_trading_allowed, require_signal_only
from usstocks.journal import UsJournal
from usstocks.models import Bar, TradeSignal, validate_symbol


def test_sql_injection_protection_in_journal(tmp_path):
    db_file = tmp_path / "sec_journal.sqlite"
    journal = UsJournal(str(db_file))
    
    # Attempt SQL injection via date, symbol, or command payload
    sqli_date = "2026-08-27'; DROP TABLE us_signals; --"
    journal.ensure_session(sqli_date)

    # Tables should still exist and not be dropped
    row = journal._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='us_signals'").fetchone()
    assert row is not None

    # Attempt SQL injection via latest_signal
    res = journal.latest_signal(symbol="AAPL' OR '1'='1")
    assert res is None

    # Check sessions table still exists
    row_sess = journal._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='us_sessions'").fetchone()
    assert row_sess is not None
    journal.close()


def test_symbol_validation_prevents_injection():
    with pytest.raises(ValueError):
        validate_symbol("AAPL; DROP TABLE users;")
    with pytest.raises(ValueError):
        validate_symbol("<script>alert(1)</script>")
    with pytest.raises(ValueError):
        validate_symbol("AAPL' OR '1'='1")
    with pytest.raises(ValueError):
        validate_symbol("../../etc/passwd")


def test_disabled_executor_hard_block():
    from execution.disabled_executor import OrderRequest, ExecutionDisabledError
    executor = DisabledExecutor()
    req = OrderRequest(
        symbol="AAPL",
        side="buy",
        qty=100,
        price=150.0,
    )
    with pytest.raises(ExecutionDisabledError, match="Execution is disabled"):
        executor.submit(req)


def test_guards_enforce_signal_only(monkeypatch):
    monkeypatch.setenv("PROFILE", "us_stocks_challenge")
    with pytest.raises(SystemExit):
        assert_auto_trading_allowed("test_broker")

    assert require_signal_only("test_runner") == "us_stocks_challenge"

    monkeypatch.setenv("PROFILE", "forex_legacy")
    with pytest.raises(SystemExit):
        require_signal_only("test_runner")
