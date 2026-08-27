"""Tests for Telegram rate limiting and event throttling (P1-3)."""
from unittest.mock import MagicMock
import pytest

from usstocks.models import RiskEvent, TradeSignal
from usstocks.notify import TelegramNotifier, TelegramRateLimiter


def test_rate_limiter_blocks_burst_messages():
    clock_time = 1000.0
    limiter = TelegramRateLimiter(
        max_per_second=2.0, max_per_chat_per_second=1.0, clock=lambda: clock_time
    )

    # First send should succeed
    assert limiter.can_send("chat1")
    limiter.record_send("chat1")

    # Immediate second send to same chat should be blocked
    assert not limiter.can_send("chat1")

    # After 1.1s, send to same chat should succeed
    clock_time += 1.1
    assert limiter.can_send("chat1")


def test_event_throttling_filters_duplicate_rejects():
    clock_time = 1000.0
    limiter = TelegramRateLimiter(
        throttle_window_seconds=300.0, clock=lambda: clock_time
    )

    event_key = "risk:AAPL:MAX_CONSECUTIVE_LOSSES"
    # First time -> not throttled
    assert not limiter.should_throttle_event(event_key)

    # Second time within window -> throttled
    clock_time += 60.0
    assert limiter.should_throttle_event(event_key)

    # After throttle window -> allowed again
    clock_time += 301.0
    assert not limiter.should_throttle_event(event_key)


def test_notifier_integrates_rate_limiter():
    mock_bot = MagicMock()
    mock_bot.send_text_message.return_value = True

    clock_time = 1000.0
    limiter = TelegramRateLimiter(
        throttle_window_seconds=300.0, clock=lambda: clock_time
    )
    notifier = TelegramNotifier(bot=mock_bot, rate_limiter=limiter)

    event = RiskEvent(
        ts=MagicMock(),
        code="MAX_CONSECUTIVE_LOSSES",
        allowed=False,
        reason="2 losses in a row",
        symbol="AAPL",
    )

    notifier.send_risk_event(event)
    assert mock_bot.send_text_message.call_count == 1

    # Second immediate identical risk event is throttled
    notifier.send_risk_event(event)
    assert mock_bot.send_text_message.call_count == 1
