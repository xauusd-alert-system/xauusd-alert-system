"""Failure mode tests (network drops, API errors, DB locks, data gaps) (Phase 4)."""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest
import requests

from shared.circuit_breaker import CircuitBreakerOpenError
from usstocks.data.utex_provider import UtexClient, decode_candles
from usstocks.journal import UsJournal
from usstocks.models import Bar, RiskState, TradeSignal
from usstocks.risk_engine import RiskEngine
from usstocks.strategy.vwap_pullback import evaluate, StrategyConfig


def test_utex_client_network_timeout_and_circuit_breaker(tmp_path):
    token_file = tmp_path / "tokens.json"
    token_file.write_text('{"refresh_token": "rt_123"}')

    client = UtexClient(token_file=str(token_file))

    with patch("requests.post", side_effect=requests.exceptions.Timeout("Connection timed out")):
        with pytest.raises(Exception):
            client.fetch_candle_dicts("access_token", "AAPL_ID", candles_count=10)

    assert client.circuit_breaker.failure_count >= 1


def test_utex_client_decode_handles_malformed_data():
    # Test decode_candles on empty/missing/corrupted candles
    assert decode_candles(None) == []
    assert decode_candles({}) == []
    assert decode_candles({"candles": []}) == []

    # Valid candle decoding with integer scaled prices
    valid = {
        "candles": [
            {"time": 1724760000, "open": 15000000000, "high": 15500000000, "low": 14900000000, "close": 15200000000, "volume": 1000}
        ]
    }
    decoded = decode_candles(valid)
    assert len(decoded) == 1
    assert decoded[0]["open"] == 150.0
    assert decoded[0]["high"] == 155.0


def test_strategy_handles_extreme_gaps_in_candles():
    """Verify strategy returns None without crashing when candles have large time gaps."""
    t0 = datetime(2026, 8, 27, 13, 30, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)  # 2-hour gap
    bars_1m = [
        Bar(ts=t0, open=100, high=101, low=99, close=100.5, volume=1000),
        Bar(ts=t1, open=102, high=103, low=101, close=102.5, volume=1000),
    ]

    ev = evaluate(
        symbol="AAPL",
        bars_1m=bars_1m,
        bench_1m=bars_1m,
        side="long",
        in_watchlist=True,
        asof=t1,
        cfg=StrategyConfig(),
    )
    assert not ev.signal


def test_risk_engine_handles_partial_fills_and_stopped_day():
    engine = RiskEngine.from_cfg({
        "risk": {
            "risk_per_trade_usd": 10.0,
            "personal_daily_stop_usd": -20.0,
            "max_trades_per_day": 2,
            "max_consecutive_losses": 2,
            "daily_profit_lock_usd": 20.0,
            "no_new_entries_minutes_before_close": 25,
        },
        "challenge": {"max_notional_usd": 5000.0},
    })
    
    # State with partial fill
    state = RiskState(
        session_date="2026-08-27",
        has_partial_fill=True,
        active_symbol="AAPL",
    )
    now = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    close_at = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
    decision = engine.evaluate(state, now=now, session_close_at=close_at, symbol="MSFT")
    assert not decision.allowed
    assert decision.code == "PARTIAL_FILL_ACTIVE"


def test_journal_recovers_when_reopened(tmp_path):
    db_file = tmp_path / "recovery.sqlite"
    journal1 = UsJournal(str(db_file))
    journal1.ensure_session("2026-08-27")
    sig = TradeSignal(
        symbol="AAPL",
        side="long",
        entry_low=150.0,
        entry_high=150.5,
        stop=149.0,
        tp1=152.0,
        tp2=154.0,
        risk_per_share=1.0,
        shares=10,
        notional_usd=1500.0,
        planned_risk_usd=10.0,
        grade="A",
    )
    journal1.save_signal(sig, "2026-08-27")
    journal1.close()

    # Reopen same DB
    journal2 = UsJournal(str(db_file))
    active = journal2.latest_signal(decision="pending")
    assert active is not None
    assert active["symbol"] == "AAPL"
    journal2.close()
