"""Tests for the challenge session-window helpers."""
from datetime import datetime

from challenge.windows import in_flatten_window, in_session_window

CFG = {
    "session": {"start_local": "18:30", "end_local": "00:55",
                "flatten_local": "00:45", "min_trading_days": 5},
}


def _t(h, m):
    return datetime(2026, 8, 24, h, m)


def test_session_window_inside():
    assert in_session_window(CFG, _t(18, 30)) is True
    assert in_session_window(CFG, _t(21, 0)) is True
    assert in_session_window(CFG, _t(0, 54)) is True


def test_session_window_outside():
    assert in_session_window(CFG, _t(18, 29)) is False
    assert in_session_window(CFG, _t(0, 55)) is False
    assert in_session_window(CFG, _t(12, 0)) is False


def test_flatten_window():
    assert in_flatten_window(CFG, _t(0, 44)) is False
    assert in_flatten_window(CFG, _t(0, 45)) is True
    assert in_flatten_window(CFG, _t(0, 54)) is True
    assert in_flatten_window(CFG, _t(0, 55)) is False
    assert in_flatten_window(CFG, _t(18, 30)) is False