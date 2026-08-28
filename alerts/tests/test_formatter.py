"""
Unit tests for alerts/formatter.py and alerts/telegram_bot.py gating logic.
Network calls are mocked - no real Telegram API calls are made in tests.
Run with: pytest alerts/tests/test_formatter.py -v
"""
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from alerts.formatter import compute_levels, format_signal_message, resolve_step
from alerts.telegram_bot import TelegramAlertBot
from config.loader import load_config

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


# ---------------------------------------------------------------------------
# Clean signal format (equal-step TP/SL grid)
# ---------------------------------------------------------------------------

def test_format_no_trade_message():
    signal = _sample_signal(bias="no_trade", confidence=0.0)
    msg = format_signal_message(signal)
    assert "NO TRADE" in msg
    assert "test reasoning" in msg


def test_clean_long_equal_step_grid():
    """Long: TP1 = entry + 1*step, TP2 = +2*step, TP3 = +3*step, SL = -3*step."""
    signal = _sample_signal(bias="long")
    signal["step"] = 3.0
    msg = format_signal_message(signal)
    # entry zone midpoint: (2400.0 + 2400.5) / 2 = 2400.25
    assert msg.splitlines()[0] == "ЛОНГ"
    assert "GOLD | ЗОЛОТО | XAUUSD" in msg
    assert "Зона входа: 2400.25" in msg
    assert "→ TP1: 2403.25" in msg
    assert "→ TP2: 2406.25" in msg
    assert "→ TP3: 2409.25" in msg
    assert "Стоп: 2391.25" in msg


def test_versioned_target_legs_support_tp4_and_explicit_stop():
    signal = _sample_signal(bias="long")
    signal["step"] = 3.0
    signal["target_legs"] = [
        {"price": 2403, "close_ratio": .4}, {"price": 2406, "close_ratio": .3},
        {"price": 2409, "close_ratio": .2}, {"price": 2412, "close_ratio": .1},
    ]
    signal["invalidation"] = 2395
    msg = format_signal_message(signal)
    assert "→ TP4: 2412" in msg
    assert "Стоп: 2395" in msg


def test_clean_short_mirrors_long_grid():
    """Short mirrors the long grid on the downside."""
    signal = _sample_signal(bias="short")
    signal["step"] = 3.0
    msg = format_signal_message(signal)
    assert msg.splitlines()[0] == "ШОРТ"
    assert "Зона входа: 2400.25" in msg
    assert "→ TP1: 2397.25" in msg
    assert "→ TP2: 2394.25" in msg
    assert "→ TP3: 2391.25" in msg
    assert "Стоп: 2409.25" in msg


def test_step_resolved_from_atr_field():
    """step is dynamic 1.0 * ATR when the signal carries the ATR value."""
    signal = _sample_signal(bias="long")
    signal["atr"] = 4.26
    assert resolve_step(signal) == pytest.approx(4.26)
    levels = compute_levels(signal)
    assert levels["tp1"] == pytest.approx(2400.25 + 4.26)


def test_step_derived_from_equal_step_targets():
    """Without explicit step/atr, step is derived from the signal's own targets."""
    signal = _sample_signal(bias="long")
    signal["targets"] = [2403.25, 2406.25, 2409.25]
    assert resolve_step(signal) == pytest.approx(3.0)


def test_step_derived_from_invalidation():
    """Last resort: step = |SL - entry| / 3."""
    signal = _sample_signal(bias="long")
    signal["targets"] = None
    signal["invalidation"] = 2391.25  # entry 2400.25 -> step = 9.0 / 3 = 3.0
    assert resolve_step(signal) == pytest.approx(3.0)


def test_grid_invariants_exactly_2x_and_3x():
    """TP2 is exactly 2x the TP1 distance, TP3 exactly 3x, SL exactly 3x."""
    signal = _sample_signal(bias="long")
    signal["step"] = 3.0
    levels = compute_levels(signal)
    assert levels["tp2"] - levels["tp1"] == pytest.approx(levels["tp1"] - levels["entry"])
    assert levels["tp3"] - levels["tp2"] == pytest.approx(levels["tp1"] - levels["entry"])
    assert abs(levels["sl"] - levels["entry"]) == pytest.approx(
        3.0 * (levels["tp1"] - levels["entry"])
    )


def test_prices_strip_trailing_zeros():
    """2-decimal formatting strips trailing zeros: 4271.80 -> '4271.8'."""
    signal = _sample_signal(bias="long")
    signal["step"] = 4.26
    signal["entry_zone"] = [4263.28, 4263.28]
    msg = format_signal_message(signal)
    assert "→ TP1: 4267.54" in msg
    assert "→ TP2: 4271.8" in msg
    assert "→ TP3: 4276.06" in msg
    assert "Стоп: 4250.5" in msg


def test_missing_step_info_raises():
    """A signal with no step/atr/targets/invalidation cannot build the grid."""
    signal = _sample_signal(bias="long")
    signal["step"] = None
    signal["atr"] = None
    signal["targets"] = None
    signal["invalidation"] = None
    with pytest.raises(ValueError):
        compute_levels(signal)


def test_meta_footer_absent_by_default():
    """Clean spec layout: no metadata line unless include_meta=True."""
    signal = _sample_signal(bias="long", confidence=0.85)
    signal["step"] = 3.0
    msg = format_signal_message(signal)
    assert "Conf:" not in msg
    assert "Regime:" not in msg


def test_meta_footer_appended_when_enabled():
    """include_meta=True appends a compact Conf / Regime / Session line."""
    signal = _sample_signal(bias="long", confidence=0.85)
    signal["step"] = 3.0
    msg = format_signal_message(signal, include_meta=True)
    assert "📊 Conf: 85.0% · Regime: trend_up · Session: london" in msg
    # The clean grid block is preserved above the footer.
    assert msg.splitlines()[0] == "ЛОНГ"
    assert "Стоп: 2391.25" in msg


# ---------------------------------------------------------------------------
# Telegram bot gating logic
# ---------------------------------------------------------------------------

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


@patch("alerts.telegram_bot.requests.post")
def test_send_alert_if_qualified_sends_clean_format(mock_post):
    """The message delivered to Telegram is the clean ШОРТ/ЛОНГ layout."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_post.return_value = mock_response

    bot = TelegramAlertBot(CFG, bot_token="fake_token", chat_id="fake_chat_id")
    signal = _sample_signal(bias="long", confidence=0.95)
    signal["step"] = 3.0

    sent = bot.send_alert_if_qualified(signal)
    assert sent is True
    payload = mock_post.call_args.kwargs["data"]["text"]
    assert payload.splitlines()[0] == "ЛОНГ"
    assert "Зона входа: 2400.25" in payload
    assert "→ TP1: 2403.25" in payload
    assert "Стоп: 2391.25" in payload


@patch("alerts.telegram_bot.requests.post")
def test_send_alert_meta_flag_read_from_config(mock_post):
    """alerts.include_signal_meta=true appends the metadata footer in the bot."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_post.return_value = mock_response

    cfg = dict(CFG)
    cfg["alerts"] = {**CFG["alerts"], "include_signal_meta": True}
    bot = TelegramAlertBot(cfg, bot_token="fake_token", chat_id="fake_chat_id")
    signal = _sample_signal(bias="long", confidence=0.9)
    signal["step"] = 3.0

    sent = bot.send_alert_if_qualified(signal)
    assert sent is True
    payload = mock_post.call_args.kwargs["data"]["text"]
    assert "📊 Conf: 90.0%" in payload
    assert "Regime: trend_up" in payload
