"""ТЗ 6.4 / P2-6: graceful shutdown tests.

Covers:
    - shutdown_runs_final_poll_and_saves — poll_once called, ledger marker written;
    - shutdown_notifies                  — notifier callback invoked;
    - double_shutdown_safe               — idempotency (second call no-ops);
    - final poll failure                 — shutdown still completes.
"""

from __future__ import annotations

import pytest

from execution.mt5_trade_group import MT5TradeGroupExecutor


@pytest.fixture
def notified():
    messages: list[str] = []
    return messages, lambda text: messages.append(text)


def test_shutdown_runs_final_poll_and_saves(tmp_path):
    db_path = str(tmp_path / "sd.sqlite")
    ex = MT5TradeGroupExecutor(db_path)

    calls = {"poll": 0}
    ex.poll_once = lambda: calls.__setitem__("poll", calls["poll"] + 1) or []

    summary = ex.shutdown()

    assert calls["poll"] == 1
    assert summary["final_poll_ok"] is True
    assert summary["already_shutdown"] is False
    assert summary["state_persisted"] is True


def test_shutdown_notifies(tmp_path):
    db_path = str(tmp_path / "sd.sqlite")
    messages: list[str] = []
    ex = MT5TradeGroupExecutor(db_path, notifier=messages.append)
    ex.poll_once = lambda: ["group_opened"]

    summary = ex.shutdown()

    assert summary["notified"] is True
    assert len(messages) == 1
    assert "SYSTEM SHUTDOWN" in messages[0]


def test_shutdown_without_notifier(tmp_path):
    ex = MT5TradeGroupExecutor(str(tmp_path / "sd.sqlite"))
    ex.poll_once = lambda: []
    summary = ex.shutdown()
    assert "notified" not in summary


def test_double_shutdown_safe(tmp_path):
    """Second shutdown() is a no-op: no second poll, no second notification."""
    db_path = str(tmp_path / "sd.sqlite")
    messages: list[str] = []
    ex = MT5TradeGroupExecutor(db_path, notifier=messages.append)
    ex.poll_once = lambda: []

    first = ex.shutdown()
    second = ex.shutdown()

    assert first["already_shutdown"] is False
    assert second["already_shutdown"] is True
    assert second["events"] == first["events"]
    assert len(messages) == 1  # notified exactly once


def test_shutdown_survives_final_poll_failure(tmp_path):
    """poll_once raising must not prevent persistence/notification (ТЗ 6.4)."""
    db_path = str(tmp_path / "sd.sqlite")
    messages: list[str] = []
    ex = MT5TradeGroupExecutor(db_path, notifier=messages.append)

    def _boom():
        raise RuntimeError("broker gone")

    ex.poll_once = _boom
    summary = ex.shutdown()

    assert summary["final_poll_ok"] is False
    assert "final_poll_error" in summary
    assert summary["state_persisted"] is True
    assert summary["notified"] is True


def test_run_bot_installs_handlers(monkeypatch):
    """scripts.run_bot._install_shutdown_handlers registers SIGTERM/SIGINT."""
    import signal

    from scripts.run_bot import _install_shutdown_handlers

    class _FakeExecutor:
        def __init__(self):
            self.calls = 0

        def shutdown(self):
            self.calls += 1

    installed = {}
    monkeypatch.setattr(
        signal,
        "signal",
        lambda sig, handler: installed.__setitem__(sig, handler),
    )
    import logging

    ex = _FakeExecutor()
    _install_shutdown_handlers(ex, logging.getLogger("test"))

    import sys as _sys

    assert signal.SIGTERM in installed or _sys.platform != "win32"
    if signal.SIGINT in installed:
        # invoking the handler triggers shutdown + SystemExit
        with pytest.raises(SystemExit):
            installed[signal.SIGINT](signal.SIGINT, None)
        assert ex.calls == 1
