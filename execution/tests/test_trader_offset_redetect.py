"""DST self-heal: the trader re-resolves the broker-server offset once per UTC
day (see MultiAssetMT5Trader._maybe_redetect_offset), so a DST flip over a
closed weekend does not stay stale until a restart.

Follows the local harness convention (object.__new__ without __init__), same
as the other execution tests.
"""
from __future__ import annotations

import datetime as dt

import pytest

import execution.mt5_trader as mod
from execution.mt5_trader import MultiAssetMT5Trader

_SUNDAY = dt.datetime(2026, 10, 25, 12, 0, 0, tzinfo=dt.timezone.utc)  # EEST->EET flip weekend
_MONDAY = dt.datetime(2026, 10, 26, 8, 0, 0, tzinfo=dt.timezone.utc)


class _PinnedDt:
    """Stub for execution.mt5_trader.datetime (class-level now())."""
    _now = None

    @classmethod
    def now(cls, tz=None):
        assert cls._now is not None, "pinned clock not set"
        return cls._now if tz is None else cls._now.astimezone(tz)


@pytest.fixture
def trader(monkeypatch):
    t = object.__new__(MultiAssetMT5Trader)
    t.cfg = {"market_data": {
        "server_time_offset_hours": "auto",
        "server_time_offset_hours_fallback": 3.0,
    }}
    t.server_offset_hours = 3.0
    t._offset_resolved_date = _SUNDAY.date()
    return t


def _pin(monkeypatch, now_utc: dt.datetime):
    _PinnedDt._now = now_utc
    monkeypatch.setattr("execution.mt5_trader.datetime", _PinnedDt)


def _fake_resolved(value):
    """(offset, info) tuple as produced by resolve_server_offset_detailed."""
    return value, {"mode": "detected", "reason": "test"}


def test_same_day_does_not_redetect(monkeypatch, trader):
    """Same UTC day as the startup resolve: no re-measurement at all."""
    calls = []

    def fake_resolve(md):
        calls.append(md)
        return _fake_resolved(99.0)  # would be picked up if called

    monkeypatch.setattr("execution.mt5_trader.resolve_server_offset_detailed", fake_resolve)
    _pin(monkeypatch, _SUNDAY)  # still Sunday, same date as _offset_resolved_date

    trader._maybe_redetect_offset()

    assert calls == []
    assert trader.server_offset_hours == 3.0


def test_new_day_redetects_and_overrides_fallback(monkeypatch, trader):
    """Monday's fresh tick overrides the Sunday fallback inside the trader."""
    monkeypatch.setattr("execution.mt5_trader.resolve_server_offset_detailed",
                        lambda md: _fake_resolved(2.0))
    _pin(monkeypatch, _MONDAY)

    trader._maybe_redetect_offset()

    assert trader.server_offset_hours == 2.0
    assert trader._offset_resolved_date == _MONDAY.date()


def test_new_day_measurement_unchanged_keeps_value(monkeypatch, trader):
    """Same-day value re-measured (e.g. no DST flip): offset stays, date advances."""
    monkeypatch.setattr("execution.mt5_trader.resolve_server_offset_detailed",
                        lambda md: _fake_resolved(3.0))
    _pin(monkeypatch, _MONDAY)

    trader._maybe_redetect_offset()

    assert trader.server_offset_hours == 3.0
    assert trader._offset_resolved_date == _MONDAY.date()


def test_dst_weekend_sequence_in_trader(monkeypatch, trader):
    """Full transition inside one process: Sunday fallback, Monday 2.0."""
    results = {"sun": None, "mon": None}

    def fake_resolve(md):
        # emulate the weekend guard: Sunday -> fallback, Monday -> fresh 2h
        if _PinnedDt._now == _SUNDAY:
            return _fake_resolved(3.0)
        return _fake_resolved(2.0)

    monkeypatch.setattr("execution.mt5_trader.resolve_server_offset_detailed", fake_resolve)

    _pin(monkeypatch, _SUNDAY)
    trader._offset_resolved_date = _SUNDAY.date()  # Sunday startup (fallback 3 cached)
    trader._maybe_redetect_offset()
    results["sun"] = trader.server_offset_hours

    _pin(monkeypatch, _MONDAY)
    trader._maybe_redetect_offset()
    results["mon"] = trader.server_offset_hours

    assert results == {"sun": 3.0, "mon": 2.0}
