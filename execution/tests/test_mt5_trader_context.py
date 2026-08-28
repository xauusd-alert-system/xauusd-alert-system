"""
Tests for the position entry-context journal in execution/mt5_trader.py
(record_position_context / purge_closed_position_context).

The journal (logs/live_positions.json) is the read-only side channel the
Telegram status commands (/status, /why) use to explain open trades. All tests
use tmp_path files — no real MT5, no network.
"""

import json
import os

import pytest

from execution.mt5_trader import (
    LIVE_POSITIONS_PATH,
    purge_closed_position_context,
    record_position_context,
)

SIGNAL_LONG = {
    "bias": "long",
    "confidence": 0.73,
    "regime": "trend_up",
    "reasoning_summary": "ML long 0.73 + rule vote совпали; тренд вверх, сессия london.",
    "entry_zone": [2001.5, 2002.5],
    "invalidation": 1990.0,
    "targets": [2006.0, 2010.0, 2014.0],
    "session": "london",
}

SIGNAL_SHORT = {
    "bias": "short",
    "confidence": 0.66,
    "regime": "range",
    "reasoning_summary": "Отказ от верхней границы диапазона; ML short 0.66.",
    "entry_zone": [99.8, 100.2],
    "invalidation": 103.0,
    "targets": [98.0, 96.0, 94.0],
    "session": "new_york",
}


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_record_creates_file_from_scratch(tmp_path):
    path = str(tmp_path / "logs" / "live_positions.json")  # nested dir is created
    record_position_context(123456, "XAUUSD", SIGNAL_LONG, path=path)

    data = _read(path)
    assert set(data.keys()) == {"123456"}
    entry = data["123456"]
    assert entry["asset_key"] == "XAUUSD"
    assert entry["bias"] == "long"
    assert entry["confidence"] == 0.73
    assert entry["regime"] == "trend_up"
    assert entry["reasoning_summary"] == SIGNAL_LONG["reasoning_summary"]
    assert entry["entry_zone"] == [2001.5, 2002.5]
    assert entry["invalidation"] == 1990.0
    assert entry["targets"] == [2006.0, 2010.0, 2014.0]
    assert entry["session"] == "london"
    assert "opened_at_utc" in entry  # ISO timestamp recorded at entry


def test_record_appends_without_losing_existing(tmp_path):
    path = str(tmp_path / "live_positions.json")
    record_position_context(1, "XAUUSD", SIGNAL_LONG, path=path)
    record_position_context(2, "EURUSD", SIGNAL_SHORT, path=path)

    data = _read(path)
    assert set(data.keys()) == {"1", "2"}
    assert data["1"]["asset_key"] == "XAUUSD"
    assert data["2"]["asset_key"] == "EURUSD"


def test_record_overwrites_same_ticket(tmp_path):
    path = str(tmp_path / "live_positions.json")
    record_position_context(1, "XAUUSD", SIGNAL_LONG, path=path)
    record_position_context(1, "XAUUSD", SIGNAL_SHORT, path=path)

    data = _read(path)
    assert len(data) == 1
    assert data["1"]["bias"] == "short"


def test_record_recovers_from_corrupted_file(tmp_path):
    path = str(tmp_path / "live_positions.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json!!!")

    record_position_context(7, "XAUUSD", SIGNAL_LONG, path=path)
    data = _read(path)
    assert set(data.keys()) == {"7"}


def test_purge_removes_only_target_ticket(tmp_path):
    path = str(tmp_path / "live_positions.json")
    record_position_context(1, "XAUUSD", SIGNAL_LONG, path=path)
    record_position_context(2, "XAUUSD", SIGNAL_SHORT, path=path)
    record_position_context(3, "EURUSD", SIGNAL_LONG, path=path)

    purge_closed_position_context(2, path=path)

    data = _read(path)
    assert set(data.keys()) == {"1", "3"}


def test_purge_missing_ticket_is_noop(tmp_path):
    path = str(tmp_path / "live_positions.json")
    record_position_context(1, "XAUUSD", SIGNAL_LONG, path=path)

    purge_closed_position_context(999, path=path)
    assert set(_read(path).keys()) == {"1"}


def test_purge_missing_file_is_noop(tmp_path):
    purge_closed_position_context(1, path=str(tmp_path / "does_not_exist.json"))
    assert not (tmp_path / "does_not_exist.json").exists()


def test_atomic_write_leaves_no_tmp_and_uses_replace(tmp_path, monkeypatch):
    """Write goes to <path>.tmp first and is then os.replace()-ed onto the
    target, so a reader never sees a half-written file. Also: if the replace
    itself fails, the previously written file content survives untouched."""
    path = str(tmp_path / "live_positions.json")
    record_position_context(1, "XAUUSD", SIGNAL_LONG, path=path)

    replace_calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("execution.mt5_trader.os.replace", spy_replace)
    record_position_context(2, "EURUSD", SIGNAL_SHORT, path=path)

    assert replace_calls, "record_position_context must publish via os.replace"
    tmp_src, dst = replace_calls[-1]
    assert tmp_src == path + ".tmp"
    assert dst == path
    # No leftover temp files after publish
    assert not os.path.exists(path + ".tmp")
    assert set(_read(path).keys()) == {"1", "2"}

    # If os.replace raises mid-publish, the old file content stays intact.
    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr("execution.mt5_trader.os.replace", boom)
    with pytest.raises(OSError):
        record_position_context(3, "EURUSD", SIGNAL_LONG, path=path)
    assert set(_read(path).keys()) == {"1", "2"}  # unchanged, not truncated


def test_default_path_constant_points_at_logs():
    assert (
        LIVE_POSITIONS_PATH.endswith(os.path.join("logs", "live_positions.json"))
        or LIVE_POSITIONS_PATH == "logs/live_positions.json"
    )
