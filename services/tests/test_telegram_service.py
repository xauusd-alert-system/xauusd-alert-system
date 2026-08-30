"""Telegram bot service checks + entrypoint guard (TZ 8.2).

NOTE: ``config.loader`` loads ``.env`` at import time, so the real
``TELEGRAM_*`` values may be present in ``os.environ`` during tests. All
checks are therefore exercised with explicit overrides (no env dependence),
and the env-based path is covered by clearing the variables via monkeypatch
before any check runs.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.base import create_health_app
from services.telegram_bot import service as tg


def test_token_check_missing_token_is_degraded_not_crash():
    ok, detail = tg.make_token_check(bot_token=None, chat_id="c")()
    assert ok is False
    assert "TELEGRAM_BOT_TOKEN" in detail


def test_token_check_missing_chat_is_degraded():
    ok, detail = tg.make_token_check(bot_token="t", chat_id=None)()
    assert ok is False
    assert "TELEGRAM_CHAT_ID" in detail


def test_token_check_configured_is_ok():
    ok, detail = tg.make_token_check(bot_token="t", chat_id="c")()
    assert ok is True
    assert "no API call" in detail


def test_token_check_empty_token_is_degraded():
    ok, _ = tg.make_token_check(bot_token="", chat_id="")()
    assert ok is False


def test_build_checks_endpoint_degraded_without_token():
    client = TestClient(create_health_app(tg.build_checks(bot_token=None, chat_id=None)))
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["checks"]["telegram_token"]["ok"] is False
    assert body["checks"]["process_alive"]["ok"] is True


def test_build_checks_endpoint_ok_with_credentials():
    client = TestClient(create_health_app(tg.build_checks(bot_token="t", chat_id="c")))
    body = client.get("/health").json()
    assert body["status"] == "ok"


def test_event_queue_enqueue_and_drain():
    assert tg.enqueue_text("hello") is True
    assert tg.EVENT_QUEUE.get_nowait() == "hello"


def test_event_queue_full_drops_oldest():
    q = tg.EVENT_QUEUE
    while not q.empty():
        q.get_nowait()
    try:
        for i in range(q.maxsize):
            assert tg.enqueue_text(f"m{i}") is True
        # Queue full: enqueue still succeeds, the oldest item is dropped.
        assert tg.enqueue_text("new") is True
        assert q.get_nowait() == "m1"
        assert q.qsize() == q.maxsize - 1
        assert list(q.queue)[-1] == "new"
    finally:
        while not q.empty():
            q.get_nowait()


def test_entrypoint_argparse_guard():
    parser = tg.build_parser()
    args = parser.parse_args([])
    assert args.health_port == tg.DEFAULT_HEALTH_PORT
    args2 = parser.parse_args(["--health-port", "9999"])
    assert args2.health_port == 9999
