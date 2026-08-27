"""Tests for RawTelegramTransport and Telegram bot interaction (Phase 4)."""
import json
import time
from unittest.mock import MagicMock, patch
import pytest

from alerts.us_commands import UsCommandsController
from usstocks.dispatcher import TelegramUpdateDispatcher
from usstocks.journal import UsJournal
from usstocks.models import RiskState
from usstocks.transport import RawTelegramTransport


def test_transport_send_message_and_answer_callback():
    transport = RawTelegramTransport("FAKE_TOKEN")
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.post", return_value=mock_resp) as mock_post:
        transport.send(
            chat_id="123",
            text="Hello World",
            reply_markup={"inline_keyboard": []},
        )
        mock_post.assert_called_once()

    with patch("requests.post", return_value=mock_resp) as mock_post:
        transport.answer_callback(callback_query_id="cb_123")
        mock_post.assert_called_once()


def test_transport_send_document(tmp_path):
    doc_path = tmp_path / "export.csv"
    doc_path.write_text("a,b,c\n1,2,3\n")
    
    transport = RawTelegramTransport("FAKE_TOKEN")
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("requests.post", return_value=mock_resp) as mock_post:
        transport.send_document(chat_id="123", path=str(doc_path))
        mock_post.assert_called_once()


def test_dispatcher_and_controller_integration(tmp_path):
    db_path = tmp_path / "bot_test.sqlite"
    journal = UsJournal(str(db_path))
    journal.ensure_session("2026-08-27")

    transport = MagicMock()
    state = RiskState(session_date="2026-08-27")

    controller = UsCommandsController(
        journal=journal,
        state=state,
        admin_id="12345",
        transport=transport,
    )
    dispatcher = TelegramUpdateDispatcher()

    # Update 1: /us_status from admin
    upd1 = {
        "update_id": 1,
        "message": {
            "text": "/us_status",
            "chat": {"id": "12345"},
            "date": int(time.time()),
        },
    }
    next_off = dispatcher.dispatch_update(upd1, controller)
    assert next_off == 2
    transport.send.assert_called()

    # Update 2: non-admin access attempt
    transport.send.reset_mock()
    upd2 = {
        "update_id": 2,
        "message": {
            "text": "/us_status",
            "chat": {"id": "99999"},
            "date": int(time.time()),
        },
    }
    next_off = dispatcher.dispatch_update(upd2, controller)
    assert next_off == 3
    transport.send.assert_called()
    assert "только для владельца" in transport.send.call_args[0][1]

    journal.close()
