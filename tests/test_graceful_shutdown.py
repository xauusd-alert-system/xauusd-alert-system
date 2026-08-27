"""Tests for Graceful Shutdown handling (P1-2)."""
from unittest.mock import MagicMock, patch
import pytest

from usstocks.bot import BotShutdownManager, main


def test_shutdown_manager_invokes_callbacks():
    mgr = BotShutdownManager()
    assert mgr.is_running

    called = []
    mgr.on_shutdown(lambda: called.append("db_closed"))
    mgr.on_shutdown(lambda: called.append("logs_flushed"))

    mgr.stop()
    assert not mgr.is_running
    assert called == ["db_closed", "logs_flushed"]

    # Stopping again is a no-op
    mgr.stop()
    assert len(called) == 2


def test_bot_main_stops_gracefully(monkeypatch):
    monkeypatch.setenv("PROFILE", "us_stocks_challenge")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token_123")

    mgr = BotShutdownManager()

    with patch("requests.get") as mock_get, patch("requests.post"):
        # Configure mock getUpdates to return empty updates then trigger stop
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": []}
        mock_resp.raise_for_status.return_value = None

        def side_effect(*args, **kwargs):
            mgr.stop()  # trigger shutdown after first poll
            return mock_resp

        mock_get.side_effect = side_effect

        exit_code = main(shutdown_manager=mgr)
        assert exit_code == 0
        assert not mgr.is_running
