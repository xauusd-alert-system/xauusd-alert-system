"""Tests for the runner <-> StealthExecutionEngine bridge.

Covers: ET-time conversion across DST, legacy Signal -> ORBSignal adaptation,
build_engine gating on challenge.stealth.enabled, and the engine branches of
the runner's _handle_signals / _manage_positions (via a stub connector, so no
browser is needed).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from challenge.stealth import runner_bridge as bridge
from challenge.strategy import Signal as LegacySignal
from tests.builder import StubConnector as _StubConnector, build_risk_object


class _FakeEngine:
    """Minimal stand-in for StealthExecutionEngine."""

    def __init__(self):
        self.session = _FakeSession()
        self.calls = {"plan": None, "manage": [], "record": 0}
        self.record_action_calls = 0

    def record_action(self):
        self.record_action_calls += 1

    def process_signal(self, *a, **k):
        return self.calls["plan"]

    def manage_position(self, *a, **k):
        return self.calls["manage"]


class _FakeSession:
    def record_trade(self):
        pass
    def mark_positions_closed(self):
        pass


# _StubConnector imported from tests.builder (shared with all test suites)


# ---------------------------------------------------------------------------
# now_et
# ---------------------------------------------------------------------------

def test_now_et_edt():
    # 2026-08-25 is EDT (offset -4): 15:30 UTC -> 11:30 ET.
    et = bridge.now_et(datetime(2026, 8, 25, 15, 30, 0))
    assert (et.hour, et.minute) == (11, 30)


def test_now_et_est():
    # 2026-01-10 is EST (offset -5): 15:30 UTC -> 10:30 ET.
    et = bridge.now_et(datetime(2026, 1, 10, 15, 30, 0))
    assert (et.hour, et.minute) == (10, 30)


# ---------------------------------------------------------------------------
# adapt_signal
# ---------------------------------------------------------------------------

def test_adapt_legacy_signal():
    s = LegacySignal(symbol="TSLA", bias="long", entry=250.0, stop=248.0,
                     tp=254.0, volume_ratio=1.5)
    orb = bridge.adapt_signal(s)
    assert orb.symbol == "TSLA"
    assert orb.bias == "long"
    assert orb.entry == 250.0
    assert orb.stop == 248.0
    assert orb.tp == 254.0
    assert orb.volume_ratio == 1.5
    # legacy Signal carries field 'session_bucket', defaults fill range/gap.
    assert orb.range_pct == bridge._FALLBACK_RANGE_PCT
    assert orb.gap_pct == bridge._FALLBACK_GAP_PCT
    # range_high/low derived for a long: high=entry, low=stop.
    assert orb.range_high == 250.0
    assert orb.range_low == 248.0


def test_adapt_short_derives_range():
    s = LegacySignal(symbol="AAPL", bias="short", entry=200.0, stop=202.0,
                     tp=194.0, volume_ratio=2.0)
    orb = bridge.adapt_signal(s)
    assert orb.bias == "short"
    assert orb.range_high == 202.0  # stop above for short
    assert orb.range_low == 200.0   # entry


# ---------------------------------------------------------------------------
# build_engine gating
# ---------------------------------------------------------------------------

def test_build_engine_none_when_disabled():
    cfg = {"challenge": {"stealth": {"enabled": False}}}
    assert bridge.build_engine(cfg) is None


def test_build_engine_when_enabled():
    cfg = {"challenge": {
        "stealth": {
            "enabled": True,
            "tickers": ["TSLA"],
            "orb_range_start": "09:30", "orb_range_end": "09:45",
            "orb_entry_start": "09:45", "orb_entry_end": "10:30",
        },
        "risk": {"start_balance": 1000.0, "max_leverage": 5},
        "session": {"start_local": "09:30", "end_local": "16:00"},
    }}
    engine = bridge.build_engine(cfg)
    assert engine is not None
    assert engine.session is not None


# ---------------------------------------------------------------------------
# execute_plan / execute_actions
# ---------------------------------------------------------------------------

def test_execute_plan_places_order_and_sets_stop(monkeypatch):
    conn = _StubConnector()
    monkeypatch.setattr(bridge, "_humanized_sleep", lambda s: None)
    plan = {"symbol": "TSLA", "bias": "long", "shares": 5, "entry": 250.0,
            "stop": 248.0, "tp": 254.0, "delay": 0.0}
    ok = bridge.execute_plan(conn, plan)
    assert ok is True
    assert conn.orders == [("TSLA", "buy", 5)]
    assert conn.stops == [("TSLA", 248.0)]


def test_execute_plan_rejects_bad_qty():
    conn = _StubConnector()
    plan = {"symbol": "TSLA", "bias": "long", "shares": 0, "entry": 250.0, "stop": 248.0}
    assert bridge.execute_plan(conn, plan) is False
    assert conn.orders == []


def test_execute_actions_applies_management():
    conn = _StubConnector()
    position = {"symbol": "TSLA", "qty": 10}
    actions = [
        {"action": "modify_stop", "new_stop": 102.0, "delay": 0.0},
        {"action": "partial_close", "shares": 3, "delay": 0.0},
    ]
    import time as _t
    bridge.execute_actions(conn, position, actions)
    assert conn.stops == [("TSLA", 102.0)]
    assert conn.partials == [("TSLA", 3)]


# ---------------------------------------------------------------------------
# Full runner integration (stub engine + stub connector)
# ---------------------------------------------------------------------------

def _build_state():
    return {"managed": {}, "day": "2026-08-25", "trading_days": 0}


def test_manage_positions_engine_route_partial_close(monkeypatch):
    """When the engine returns a partial-exit action, the position is updated."""
    import challenge.runner as runner

    conn = _StubConnector()
    monkeypatch.setattr(bridge, "_humanized_sleep", lambda s: None)

    engine = _FakeEngine()
    engine.calls["manage"] = [
        {"action": "partial_close", "symbol": "TSLA", "shares": 2, "delay": 0.0},
    ]

    state = _build_state()
    state["managed"]["TSLA"] = {
        "side": "long", "qty": 5, "entry": 100.0, "stop": 99.0, "tp": 106.0,
        "remaining_shares": 5, "partial_closed": False,
    }
    quotes = {"TSLA": {"last": 102.0}}

    runner._manage_positions(conn, state, quotes, {}, engine=engine,
                             now_et=datetime(2026, 8, 25, 10, 0), floating_pnl=5.0)

    assert conn.partials == [("TSLA", 2)]
    assert state["managed"]["TSLA"]["partial_closed"] is True
    assert state["managed"]["TSLA"]["remaining_shares"] == 3


def test_manage_positions_legacy_path_when_no_engine(monkeypatch):
    """Engine None -> the legacy stop/TP check runs unchanged."""
    import challenge.runner as runner

    conn = _StubConnector()
    state = _build_state()
    # A long position where last drops below the stop.
    state["managed"]["TSLA"] = {
        "side": "long", "qty": 5, "entry": 100.0, "stop": 99.0, "tp": 106.0,
        "remaining_shares": 5, "partial_closed": False,
    }
    quotes = {"TSLA": {"last": 98.5}}

    runner._manage_positions(conn, state, quotes, {})

    # Legacy path closes at the stop.
    assert state["managed"] == {}



def test_handle_signals_legacy_path_when_no_engine(monkeypatch):
    import challenge.runner as runner

    conn = _StubConnector()
    signal = LegacySignal(symbol="TSLA", bias="long", entry=100.0, stop=99.0,
                          tp=103.0, volume_ratio=1.0, session_bucket="prime")
    monkeypatch.setattr(bridge, "_humanized_sleep", lambda s: None)

    # Legacy path: signal accepted, order placed, positional qty from risk.
    state = _build_state()
    # risk.position_size(strict) returns int shares.

    snap = {"quotes": {"TSLA": {"last": 100.0}}, "equity": 1000.0,
            "pnl": 0.0, "positions": []}
    runner._handle_signals(
        conn, _make_risk(), _StubStrategy([signal]), state, snap,
        datetime(2026, 8, 25, 14, 0),
        {"session": {"start_local": "18:30", "end_local": "00:55",
                     "flatten_local": "00:45"}})
    assert state["managed"]["TSLA"]["qty"] == 5
    # Legacy path passes the bias verbatim ('long'), matching the connector.
    assert conn.orders == [("TSLA", "long", 5)]


class _StubStrategy:
    def __init__(self, signals):
        self._signals = signals

    def update(self, quotes, now):
        return self._signals


def _make_risk():
    return build_risk_object(max_open_positions=2, daily_loss_stop=25,
                             per_trade_risk_usd=5.0, position_size_value=5)


def test_handle_signals_engine_skips_on_none_plan(monkeypatch):
    import challenge.runner as runner

    conn = _StubConnector()
    signal = LegacySignal(symbol="TSLA", bias="long", entry=100.0, stop=99.0,
                          tp=103.0, volume_ratio=1.0, session_bucket="prime")
    monkeypatch.setattr(bridge, "_humanized_sleep", lambda s: None)

    engine = _FakeEngine()
    engine.calls["plan"] = None  # engine rejects -> silent skip

    state = _build_state()
    snap = {"quotes": {"TSLA": {"last": 100.0}}, "equity": 1000.0,
            "pnl": 0.0, "positions": []}
    runner._handle_signals(
        conn, _make_risk(), _StubStrategy([signal]), state, snap,
        datetime(2026, 8, 25, 14, 0),
        {"session": {"start_local": "18:30", "end_local": "00:55",
                     "flatten_local": "00:45"}},
        engine=engine, now_et=datetime(2026, 8, 25, 10, 0),
        equity=1000.0, day_start=1000.0, total_start=1000.0,
        floating_pnl=0.0)
    # No order, no managed position.
    assert conn.orders == []
    assert state["managed"] == {}


def test_handle_signals_engine_executes_plan(monkeypatch):
    import challenge.runner as runner

    conn = _StubConnector()
    signal = LegacySignal(symbol="TSLA", bias="long", entry=100.0, stop=99.0,
                          tp=103.0, volume_ratio=1.0, session_bucket="prime")
    monkeypatch.setattr(bridge, "_humanized_sleep", lambda s: None)

    engine = _FakeEngine()
    engine.calls["plan"] = {"symbol": "TSLA", "bias": "long", "shares": 7,
                            "entry": 101.0, "stop": 99.5, "tp": 105.0,
                            "delay": 0.0}

    state = _build_state()
    snap = {"quotes": {"TSLA": {"last": 100.0}}, "equity": 1000.0,
            "pnl": 0.0, "positions": []}
    runner._handle_signals(
        conn, _make_risk(), _StubStrategy([signal]), state, snap,
        datetime(2026, 8, 25, 14, 0),
        {"session": {"start_local": "18:30", "end_local": "00:55",
                     "flatten_local": "00:45"}},
        engine=engine, now_et=datetime(2026, 8, 25, 10, 0),
        equity=1000.0, day_start=1000.0, total_start=1000.0,
        floating_pnl=0.0)
    # Engine plan's qty/stop used, not the legacy signal's.
    assert conn.orders == [("TSLA", "buy", 7)]
    assert conn.stops == [("TSLA", 99.5)]
    assert state["managed"]["TSLA"]["qty"] == 7
    assert state["managed"]["TSLA"]["entry"] == 101.0
    assert state["managed"]["TSLA"]["stop"] == 99.5
    assert state["managed"]["TSLA"]["partial_closed"] is False
