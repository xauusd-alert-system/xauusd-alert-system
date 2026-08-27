"""Tests for the trading blackout (owner request 2026-08-19).

The trader must be OFF while the market is inactive: weekend window
(Fri 21:00 UTC -> Sun 21:00 UTC), the night no-volatility window
(22:00 -> 08:00 UTC, new entries skipped, crosses midnight), and an
optional one-off manual halt covering unattended stretches.
"""
import types
from datetime import datetime, timezone

import pytest

from execution import mt5_trader as trader_mod
from execution.mt5_trader import MultiAssetMT5Trader


def _cfg(**blackout_overrides):
    bo = {
        "enabled": True,
        "daily_break_utc": ["22:00", "08:00"],
        "weekend": {"start_dow": 4, "start_utc": "21:00",
                    "end_dow": 6, "end_utc": "21:00"},
        "flatten_before_minutes": 10,
    }
    bo.update(blackout_overrides)
    return {"execution": {"trading_blackout": bo}}


def _trader(cfg):
    t = object.__new__(MultiAssetMT5Trader)
    t.cfg = cfg
    t.magic_number = 777111
    t.dry_run = False
    t._init_blackout()
    t._blackout_flattened = False
    return t


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def test_weekend_window_halts_and_resumes():
    t = _trader(_cfg())
    assert t._blackout_status(_utc("2026-08-19 20:00"))[0] is False   # Wed
    halted, reason, resume = t._blackout_status(_utc("2026-08-21 22:00"))  # Fri
    assert halted is True
    assert "weekend" in reason
    assert resume == _utc("2026-08-23 21:00")                           # Sun
    assert t._blackout_status(_utc("2026-08-22 12:00"))[0] is True      # Sat
    assert t._blackout_status(_utc("2026-08-23 22:00"))[0] is False     # Sun after


def test_manual_halt_overrides_weekend():
    t = _trader(_cfg(manual_halt_until_utc="2026-08-24 07:00"))
    halted, reason, _ = t._blackout_status(_utc("2026-08-19 22:00"))
    assert halted is True
    assert "manual halt" in reason
    # After the manual halt expires the recurring weekend window applies.
    assert t._blackout_status(_utc("2026-08-24 08:00"))[0] is False     # Mon
    assert t._blackout_status(_utc("2026-08-28 22:00"))[0] is True      # Fri


def test_disabled_blackout_never_halts():
    t = _trader(_cfg(enabled=False))
    assert t._blackout_status(_utc("2026-08-21 22:00"))[0] is False
    assert t._in_daily_break(_utc("2026-08-21 21:30")) is False


def test_daily_break_window():
    t = _trader(_cfg())
    assert t._in_daily_break(_utc("2026-08-19 21:59")) is False
    assert t._in_daily_break(_utc("2026-08-19 22:00")) is True
    assert t._in_daily_break(_utc("2026-08-20 00:30")) is True
    assert t._in_daily_break(_utc("2026-08-20 07:59")) is True
    assert t._in_daily_break(_utc("2026-08-20 08:00")) is False
    assert t._in_daily_break(_utc("2026-08-20 14:00")) is False


def test_daily_break_plain_window_without_midnight_crossing():
    t = _trader(_cfg(daily_break_utc=["11:00", "13:00"]))
    assert t._in_daily_break(_utc("2026-08-20 10:59")) is False
    assert t._in_daily_break(_utc("2026-08-20 12:00")) is True
    assert t._in_daily_break(_utc("2026-08-20 13:00")) is False


def test_flatten_all_positions_closes_every_position(monkeypatch):
    t = _trader(_cfg())
    pos1 = types.SimpleNamespace(ticket=1, symbol="GOLD", type=0, volume=0.34)
    pos2 = types.SimpleNamespace(ticket=2, symbol="EURUSD", type=1, volume=0.33)
    monkeypatch.setattr(trader_mod.mt5, "initialize", lambda *a, **k: True)
    monkeypatch.setattr(trader_mod, "positions_get_by_magic",
                        lambda *a, **k: [pos1, pos2])
    monkeypatch.setattr(trader_mod.mt5, "symbol_info_tick", lambda s: (
        types.SimpleNamespace(bid=2400.0, ask=2400.1)
        if s == "GOLD" else types.SimpleNamespace(bid=1.1000, ask=1.1001)))
    closed = []
    # _close_partial_position gained keyword args over time (e.g.
    # quiet_market_closed for blackout passes); accept **kwargs so the
    # harness tracks the current call signature.
    t._close_partial_position = lambda pos, price, volume, label, **kwargs: (
        closed.append((pos.ticket, volume, label)) or True)
    t._flatten_all_positions("weekend blackout")
    assert sorted(c[0] for c in closed) == [1, 2]
    assert all(c[1] in (0.34, 0.33) for c in closed)
    assert all(c[2] == "blackout-halt" for c in closed)


def test_flatten_with_no_positions_is_noop(monkeypatch):
    t = _trader(_cfg())
    monkeypatch.setattr(trader_mod.mt5, "initialize", lambda *a, **k: True)
    monkeypatch.setattr(trader_mod, "positions_get_by_magic",
                        lambda *a, **k: None)
    def _boom(*a, **k):
        raise AssertionError("should not close anything")
    t._close_partial_position = _boom
    t._flatten_all_positions("weekend blackout")