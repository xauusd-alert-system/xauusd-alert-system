"""
Tests for control-bot polling security and resilience:

- Token redaction (alerts/control_bot.py redact_bot_token / TelegramControlBot._redact):
  requests exceptions embed the request URL, which contains the raw bot token —
  logging them raw leaks control of the bot (leak observed in logs/mt5_trader.err
  on 2026-08-30).
- Cross-process poll lock (acquire_token_poll_lock): exactly one getUpdates
  consumer per bot token; a second process must decline, not fight (409 storm).
- Alert-send retry (alerts/telegram_bot.py _post_with_retry): transient
  ConnectionError behind the VPN must not silently lose a trade-close alert.

Everything runs on mocks: no network to the Telegram Bot API (CI has none).
"""

import logging
import os
from pathlib import Path

import pytest
import requests

from alerts import telegram_bot as tb
from alerts.control_bot import (
    TelegramControlBot,
    _token_lock_path,
    acquire_token_poll_lock,
    redact_bot_token,
)

TOKEN = "8841075807:TEST-TOKEN-NOT-REAL"
URL_WITH_TOKEN = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset=0&timeout=30"


# ---------------------------------------------------------------------------
# Token redaction (security): bot<token>/method -> bot***REDACTED***/method
# ---------------------------------------------------------------------------


def test_redact_bot_token_redacts_url_form_and_bare_token():
    exc = f"Poll error: HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: {URL_WITH_TOKEN}"
    out = redact_bot_token(exc, TOKEN)
    assert TOKEN not in out, "raw token must never survive redaction"
    assert "bot***REDACTED***/getUpdates" in out


def test_redact_bot_token_redacts_bare_token_outside_url():
    out = redact_bot_token(f"connection dropped mid-token {TOKEN} oops", TOKEN)
    assert TOKEN not in out
    assert "<REDACTED>" in out


def test_redact_bot_token_noop_on_empty_inputs():
    assert redact_bot_token("", TOKEN) == ""
    assert redact_bot_token("clean text", "") == "clean text"


def test_redact_bot_token_matches_telegram_alert_bot_pattern():
    """Same visible pattern as TelegramAlertBot._redact: no raw token anywhere."""
    from alerts.telegram_bot import TelegramAlertBot

    alert_bot = TelegramAlertBot(cfg={}, bot_token=TOKEN, chat_id="1")
    control_out = redact_bot_token(URL_WITH_TOKEN, TOKEN)
    alert_out = alert_bot._redact(URL_WITH_TOKEN)
    assert TOKEN not in control_out and TOKEN not in alert_out


def test_poll_loop_logs_redacted_error(monkeypatch, caplog):
    """The /getUpdates poll loop must never log the raw token from a requests
    exception (this exact leak appeared in logs/mt5_trader.err)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "4242")
    trader = type("T", (), {"magic_number": 1, "dry_run": True, "pipelines": {}, "cfg": {}})()
    bot = TelegramControlBot(trader)

    def boom():
        # Stop the loop from inside so exactly one failing iteration runs,
        # then raise with the poisoned URL.
        bot._stop.set()
        raise requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: {URL_WITH_TOKEN}"
        )

    monkeypatch.setattr(bot, "_get_updates", boom)
    monkeypatch.setattr("alerts.control_bot.time.sleep", lambda *_: None)

    # The control_bot logger may not reach caplog's root handler by default;
    # force propagation and capture level explicitly.
    logging.getLogger("control_bot").propagate = True
    caplog.set_level(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger="control_bot"):
        bot._poll_loop()

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "Poll error" in joined
    assert TOKEN not in joined, "raw token leaked into the poll-error log line"
    assert "bot***REDACTED***/getUpdates" in joined


# ---------------------------------------------------------------------------
# Cross-process poll lock: one getUpdates consumer per bot token
# ---------------------------------------------------------------------------


@pytest.fixture()
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_POLL_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


def test_poll_lock_prevents_duplicate(lock_dir, monkeypatch):
    """Two processes, one token: the first to acquire wins; the second refuses
    to poll instead of causing a Telegram 409 Conflict storm."""
    # Process A (this one, real pid) claims the lock.
    assert acquire_token_poll_lock(TOKEN) is True
    pidfile = Path(os.path.join(_token_lock_path(TOKEN), "pid"))
    assert int(pidfile.read_text()) == os.getpid()

    # Process B (simulated live foreign pid): lock exists and owner is alive
    # -> B must decline.
    foreign_pid = os.getpid() + 1
    monkeypatch.setattr("alerts.control_bot._pid_alive", lambda pid: pid == foreign_pid)
    pidfile.write_text(str(foreign_pid))
    assert acquire_token_poll_lock(TOKEN) is False, "second poller must refuse"


def test_poll_lock_reclaims_stale_owner(lock_dir, monkeypatch):
    """A lock whose owner pid is dead is stale: the new process may reclaim it."""
    assert acquire_token_poll_lock(TOKEN) is True
    monkeypatch.setattr("alerts.control_bot._pid_alive", lambda pid: False)
    assert acquire_token_poll_lock(TOKEN) is True, "dead owner -> lock is stale, reclaim"
    pidfile = Path(os.path.join(_token_lock_path(TOKEN), "pid"))
    assert int(pidfile.read_text()) == os.getpid()


def test_poll_lock_path_never_contains_raw_token(lock_dir):
    """The lock filename is a hash: the token must not leak via the filesystem."""
    path = _token_lock_path(TOKEN)
    assert TOKEN not in path
    assert path.endswith(".lock")


# ---------------------------------------------------------------------------
# Alert-send retry (_post_with_retry): transient errors don't lose alerts
# ---------------------------------------------------------------------------


class FakeResponse:
    ok = True
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        pass


def _retry_bot(monkeypatch):
    """TelegramAlertBot with time.sleep stubbed so retries are instant."""
    monkeypatch.setattr(tb.time, "sleep", lambda *_: None)
    return tb.TelegramAlertBot(cfg={}, bot_token=TOKEN, chat_id="1")


def test_post_with_retry_succeeds_after_transient_error(monkeypatch):
    """ConnectionError on attempt 1 -> success on attempt 2; the alert survives."""
    bot = _retry_bot(monkeypatch)
    calls = {"n": 0}

    def flaky_post(url, data=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("ConnectionResetError(10054)")
        return FakeResponse()

    monkeypatch.setattr(tb.requests, "post", flaky_post)
    resp = bot._post_with_retry("https://example.invalid", {"a": 1})
    assert resp is not None and calls["n"] == 2, "must succeed on the 2nd attempt"


def test_post_with_retry_fails_after_max_retries(monkeypatch):
    """3 ConnectionErrors -> the final error propagates (caller logs + fails soft)."""
    bot = _retry_bot(monkeypatch)
    calls = {"n": 0}

    def dead_post(url, data=None, timeout=None):
        calls["n"] += 1
        raise requests.exceptions.ConnectionError(f"drop #{calls['n']}")

    monkeypatch.setattr(tb.requests, "post", dead_post)
    with pytest.raises(requests.exceptions.ConnectionError):
        bot._post_with_retry("https://example.invalid", {"a": 1})
    assert calls["n"] == 3, "default policy: exactly 3 attempts, then raise"


def test_post_with_retry_does_not_retry_http_4xx(monkeypatch):
    """Retries are for *transport* errors only — a 400 answer is returned as-is
    (the caller's Markdown-fallback logic decides what to do with it)."""
    bot = _retry_bot(monkeypatch)
    calls = {"n": 0}

    def bad_request_post(url, data=None, timeout=None):
        calls["n"] += 1
        resp = FakeResponse()
        resp.ok = False
        resp.status_code = 400
        return resp

    monkeypatch.setattr(tb.requests, "post", bad_request_post)
    resp = bot._post_with_retry("https://example.invalid", {"a": 1})
    assert calls["n"] == 1 and resp.status_code == 400
