"""
Unit tests for scripts/run_scheduler.py - timing math and single-pass execution.
The infinite loop (main()) is NOT tested directly; run_once() and the sleep-time
calculation are the testable, deterministic units.
Run with: pytest scripts/tests/test_scheduler.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from alerts.telegram_bot import TelegramAlertBot
from config.loader import load_config
from realtime.pipeline import RealtimePipeline
from scripts.run_scheduler import run_once, seconds_until_next_candle_close

CFG = load_config()


def test_seconds_until_next_candle_close_is_positive_and_bounded():
    sleep_s = seconds_until_next_candle_close("M15", buffer_seconds=5)
    assert 0 < sleep_s <= 900 + 5


def test_seconds_until_next_candle_close_respects_buffer():
    """Sleep time must always include at least the buffer after the boundary."""
    sleep_s_no_buffer = seconds_until_next_candle_close("M15", buffer_seconds=0)
    sleep_s_with_buffer = seconds_until_next_candle_close("M15", buffer_seconds=10)
    assert sleep_s_with_buffer >= sleep_s_no_buffer


@patch("alerts.telegram_bot.requests.post")
def test_run_once_logs_signal_and_returns_dict(mock_post, tmp_path):
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    from data.signal_log import init_schema, read_signal_history
    db_path = str(tmp_path / "test.db")
    init_schema(db_path)

    pipeline = RealtimePipeline(cfg=CFG, model_path=None, data_mode="mock")
    bot = TelegramAlertBot(CFG, bot_token="fake", chat_id="fake")

    result = run_once(pipeline, bot, db_path, n_candles=300)
    assert result["bias"] in ("long", "short", "no_trade")

    history = read_signal_history(db_path)
    assert len(history) == 1


@patch("alerts.telegram_bot.requests.post")
def test_run_once_does_not_alert_when_no_trade(mock_post, tmp_path):
    from data.signal_log import init_schema
    db_path = str(tmp_path / "test.db")
    init_schema(db_path)

    pipeline = RealtimePipeline(cfg=CFG, model_path=None, data_mode="mock")
    bot = TelegramAlertBot(CFG, bot_token="fake", chat_id="fake")

    result = run_once(pipeline, bot, db_path, n_candles=300)
    if result["bias"] == "no_trade":
        assert not mock_post.called
