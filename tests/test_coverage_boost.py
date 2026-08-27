"""Comprehensive coverage boost tests for runner, notifier, session, container (Phase 4)."""
import os
import tempfile
import time
from datetime import datetime, date, time as dt_time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo
import pytest

from shared.container import build_container, BotContainer
from usstocks.journal import UsJournal
from usstocks.models import Bar, RiskEvent, RiskState, TradeSignal, WatchlistItem, PremarketSnapshot
from usstocks.notify import format_signal_message, PrintNotifier, TelegramNotifier, TelegramRateLimiter
from usstocks.scanner_loop import SignalOnlyRunner, run_forever
from usstocks.session import NySession, session_from_cfg

NY = ZoneInfo("America/New_York")


def test_session_edge_cases():
    cfg = {
        "session": {
            "timezone": "America/New_York",
            "open": "09:30",
            "close": "16:00",
            "holidays": ["2026-12-25"],
            "early_close": {"2026-11-27": "13:00"},
        }
    }
    session = session_from_cfg(cfg)

    # Weekend check
    sat = date(2026, 8, 29)
    assert not session.is_trading_day(sat)

    # Holiday check
    xmas = date(2026, 12, 25)
    assert not session.is_trading_day(xmas)

    # Early close check
    bf = date(2026, 11, 27)
    assert session.is_trading_day(bf)
    assert session.session_close(bf).time() == dt_time(13, 0)


def test_print_and_telegram_notifiers():
    pn = PrintNotifier()
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
        grade="A+",
        why=["VWAP support holding", "RVOL > 2.0"],
    )
    # Should not raise
    pn.send_signal(sig)
    pn.send_watchlist([])
    pn.send_risk_event(RiskEvent(ts=datetime.now().astimezone(), code="ALLOW", allowed=True, reason="ok"))

    # TelegramNotifier test
    mock_bot = MagicMock()
    mock_bot.send_text_message.return_value = True
    clock_time = 1000.0
    def stepping_clock():
        nonlocal clock_time
        clock_time += 1.0
        return clock_time

    rate_limiter = TelegramRateLimiter(
        max_per_second=10.0,
        max_per_chat_per_second=1.0,
        throttle_window_seconds=0.0,
        clock=stepping_clock,
    )
    tn = TelegramNotifier(bot=mock_bot, rate_limiter=rate_limiter)
    tn.send_signal(sig)
    assert mock_bot.send_text_message.called

    mock_bot.reset_mock()
    snap = PremarketSnapshot(
        symbol="TSLA",
        price=200.0,
        prev_close=195.0,
        gap_pct=2.5,
        relative_volume=2.0,
        avg_daily_dollar_volume=100000000,
        spread_pct=0.01,
    )
    tn.send_watchlist([WatchlistItem(snapshot=snap, is_tech=True)])
    assert mock_bot.send_text_message.called

    mock_bot.reset_mock()
    tn.send_risk_event(RiskEvent(ts=datetime.now().astimezone(), code="DAILY_STOP", allowed=False, reason="hit stop", symbol="AAPL"))
    assert mock_bot.send_text_message.called


def test_runner_error_handling_and_metrics(tmp_path):
    db_file = tmp_path / "runner_test.sqlite"
    journal = UsJournal(str(db_file))
    journal.ensure_session("2026-08-27")

    cfg = {
        "risk": {
            "risk_per_trade_usd": 10.0,
            "personal_daily_stop_usd": -20.0,
            "max_trades_per_day": 2,
            "max_consecutive_losses": 2,
            "daily_profit_lock_usd": 20.0,
            "no_new_entries_minutes_before_close": 25,
        },
        "challenge": {"max_notional_usd": 5000.0},
        "scanner": {"poll_seconds": 60, "max_spread_pct": 0.5},
        "us_stocks": {"tech_symbols": ["AAPL"]},
        "strategy": {},
    }
    
    mock_provider = MagicMock()
    mock_provider.get_bars.side_effect = Exception("Provider error")
    mock_notifier = MagicMock()
    
    runner = SignalOnlyRunner(
        cfg=cfg,
        provider=mock_provider,
        notifier=mock_notifier,
        watchlist=["AAPL", "UNKNOWN_SYM"],
        symbol_ids={"AAPL": "1", "QQQ": "2"},
        journal=journal,
    )
    
    now = datetime(2026, 8, 27, 10, 0, tzinfo=NY)
    # When signals disabled
    runner.signals_enabled = False
    assert runner.scan_once(now) == []
    runner.signals_enabled = True

    # When provider fails, scan_once catches exceptions internally and logs, returns empty
    signals = runner.scan_once(now)
    assert signals == []
    assert runner.metrics["total_scans"] == 1
    assert runner.metrics["last_scan_duration_ms"] >= 0

    # Test gate risk check
    allowed = runner._gate(now, "AAPL")
    assert allowed

    journal.close()


def test_dispatcher_stale_update():
    from usstocks.dispatcher import TelegramUpdateDispatcher
    dispatcher = TelegramUpdateDispatcher(stale_timeout_seconds=10)
    mock_controller = MagicMock()
    
    stale_upd = {
        "update_id": 100,
        "message": {
            "text": "/us_status",
            "chat": {"id": 123},
            "date": int(time.time()) - 500,
        }
    }
    next_off = dispatcher.dispatch_update(stale_upd, mock_controller)
    assert next_off == 101
    assert not mock_controller.handle_command.called


def test_transport_error_handling():
    from usstocks.transport import RawTelegramTransport
    transport = RawTelegramTransport("FAKE_TOKEN")
    
    with patch("requests.post", side_effect=Exception("Network error")):
        # These should catch exception and not raise
        transport.send("123", "test")
        transport.answer_callback("cb_1")
        transport.send_document("123", "nonexistent.csv")


def test_container_builder_default():
    container = build_container(custom_provider=MagicMock())
    assert isinstance(container, BotContainer)
    assert container.risk_engine is not None
    assert container.journal is not None
