"""
Unit tests for alerts/formatter.py and alerts/telegram_bot.py gating logic.
Network calls are mocked - no real Telegram API calls are made in tests.
Run with: pytest alerts/tests/test_formatter.py -v
"""
import os
import sys
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from alerts.formatter import format_signal_message
from alerts.telegram_bot import TelegramAlertBot

CFG = load_config()


def _sample_signal(bias="long", confidence=0.8):
    return {
        "bias": bias,
        "confidence": confidence,
        "entry_zone": [2400.0, 2400.5] if bias != "no_trade" else None,
        "invalidation": 2398.0 if bias != "no_trade" else None,
        "targets": [2403.0] if bias != "no_trade" else None,
        "reasoning_summary": "test reasoning",
        "regime": "trend_up",
        "session": "london",
        "timestamp_utc": 1700000000,
        "generated_at": "2026-07-24T08:00:00+00:00",
    }


def test_format_no_trade_message():
    signal = _sample_signal(bias="no_trade", confidence=0.0)
    msg = format_signal_message(signal)
    assert "NO TRADE" in msg
    assert "test reasoning" in msg


def test_format_long_message_contains_key_fields():
    signal = _sample_signal(bias="long", confidence=0.8)
    msg = format_signal_message(signal)
    assert "LONG" in msg
    assert "80.0%" in msg
    assert "2400.0" in msg
    assert "2398.0" in msg
    assert "2403.0" in msg


def test_format_short_message_direction_label():
    signal = _sample_signal(bias="short", confidence=0.75)
    msg = format_signal_message(signal)
    assert "SHORT" in msg


def test_bot_suppresses_no_trade_signal():
    bot = TelegramAlertBot(CFG, bot_token="fake_token", chat_id="fake_chat_id")
    signal = _sample_signal(bias="no_trade", confidence=0.0)
    assert bot._should_send(signal) is False


def test_bot_suppresses_below_confidence_threshold():
    bot = TelegramAlertBot(CFG, bot_token="fake_token", chat_id="fake_chat_id")
    low_conf_signal = _sample_signal(bias="long", confidence=0.1)
    assert bot._should_send(low_conf_signal) is False


def test_bot_allows_high_confidence_signal():
    bot = TelegramAlertBot(CFG, bot_token="fake_token", chat_id="fake_chat_id")
    high_conf_signal = _sample_signal(bias="long", confidence=0.95)
    assert bot._should_send(high_conf_signal) is True


def test_bot_enforces_cooldown():
    bot = TelegramAlertBot(CFG, bot_token="fake_token", chat_id="fake_chat_id")
    signal = _sample_signal(bias="long", confidence=0.95)
    bot._last_alert_ts = time.time()  # simulate an alert just sent
    assert bot._should_send(signal) is False, "Cooldown period must suppress immediate re-alert"


def test_bot_enforces_daily_cap():
    bot = TelegramAlertBot(CFG, bot_token="fake_token", chat_id="fake_chat_id")
    bot._alerts_sent_today = CFG["alerts"]["max_alerts_per_day"]
    signal = _sample_signal(bias="long", confidence=0.95)
    assert bot._should_send(signal) is False, "Daily cap must suppress further alerts"


@patch("alerts.telegram_bot.requests.post")
def test_send_alert_if_qualified_calls_api_and_updates_state(mock_post):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_post.return_value = mock_response

    bot = TelegramAlertBot(CFG, bot_token="fake_token", chat_id="fake_chat_id")
    signal = _sample_signal(bias="long", confidence=0.95)

    sent = bot.send_alert_if_qualified(signal)
    assert sent is True
    assert mock_post.called
    assert bot._alerts_sent_today == 1
    assert bot._last_alert_ts is not None


@patch("alerts.telegram_bot.requests.post")
def test_send_alert_if_qualified_skips_api_call_when_suppressed(mock_post):
    bot = TelegramAlertBot(CFG, bot_token="fake_token", chat_id="fake_chat_id")
    signal = _sample_signal(bias="no_trade", confidence=0.0)

    sent = bot.send_alert_if_qualified(signal)
    assert sent is False
    assert not mock_post.called
