"""Tests for TelegramUpdateDispatcher (P2-5)."""
from unittest.mock import MagicMock
import pytest

from usstocks.dispatcher import TelegramUpdateDispatcher


def test_dispatcher_routes_command():
    import time
    dispatcher = TelegramUpdateDispatcher(stale_timeout_seconds=600)
    mock_ctrl = MagicMock()

    upd = {
        "update_id": 101,
        "message": {
            "date": int(time.time()),
            "chat": {"id": 12345},
            "text": "/us_status",
        },
    }

    next_offset = dispatcher.dispatch_update(upd, mock_ctrl)
    assert next_offset == 102
    mock_ctrl.handle_command.assert_called_once_with("/us_status", "12345", ())


def test_dispatcher_routes_callback():
    dispatcher = TelegramUpdateDispatcher(stale_timeout_seconds=600)
    mock_ctrl = MagicMock()

    upd = {
        "update_id": 105,
        "callback_query": {
            "id": "cb_999",
            "data": "us:confirm:nonce123",
            "message": {"chat": {"id": 12345}},
        },
    }

    next_offset = dispatcher.dispatch_update(upd, mock_ctrl)
    assert next_offset == 106
    mock_ctrl.handle_callback.assert_called_once_with(
        "us:confirm:nonce123", "12345", callback_id="cb_999"
    )
