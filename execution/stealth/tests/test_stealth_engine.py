"""Tests for StealthExecutionEngine — 6 gates integration."""

from datetime import datetime, timezone, timedelta

import pytest

from execution.stealth.config import StealthConfig
from execution.stealth.engine import StealthExecutionEngine


def _make_signal(volume=0.10, entry=2000, stop=1995):
    return {
        "signal_id": "test-signal-123",
        "bias": "long",
        "entry_zone": [entry - 0.5, entry + 0.5],
        "invalidation": stop,
        "targets": [2005, 2010, 2015],
        "volume": volume,
        "price": entry,
    }


def test_engine_gates_session_check():
    config = StealthConfig(seed=42, no_trade_day_prob=0.0, daily_cap_range=(10, 10))
    engine = StealthExecutionEngine(config=config)
    # Force no-trade day off
    engine.session_sim._is_no_trade_day = False
    engine.session_sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()

    # Weekend should fail gate 1
    sat = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    signal = _make_signal()
    assert engine.process_signal(signal, sat, equity=10000) is None

    # Weekday inside session should pass gate 1
    mon = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    plan = engine.process_signal(signal, mon, equity=10000)
    # May still fail other gates, but not gate 1
    # Ensure session check passes by checking is_in_trading_session
    assert engine.session_sim.is_in_trading_session(mon) is True


def test_engine_gate_session_end_buffer():
    config = StealthConfig(seed=42, no_trade_day_prob=0.0, daily_cap_range=(10, 10), session_end_buffer_range=(600, 600))
    engine = StealthExecutionEngine(config=config)
    engine.session_sim._is_no_trade_day = False
    engine.session_sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()
    engine.session_sim._session_end_buffer_sec = 600
    engine.timer._last_order_time = None

    # 15:55 inside London buffer (ends 16:00, buffer 10 min) => should fail gate 2
    in_buffer = datetime(2026, 8, 24, 15, 55, tzinfo=timezone.utc)
    signal = _make_signal()
    assert engine.process_signal(signal, in_buffer, equity=10000) is None

    # 15:45 before buffer => should pass gate 2 (if other gates pass)
    before_buffer = datetime(2026, 8, 24, 15, 45, tzinfo=timezone.utc)
    # Need to ensure min gap ok
    engine.timer._last_order_time = None
    plan = engine.process_signal(signal, before_buffer, equity=10000)
    assert plan is not None


def test_engine_gate_min_gap():
    config = StealthConfig(seed=42, no_trade_day_prob=0.0, daily_cap_range=(10, 10))
    engine = StealthExecutionEngine(config=config)
    engine.session_sim._is_no_trade_day = False
    engine.session_sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()
    engine.session_sim._daily_cap = 10
    engine.session_sim._orders_today = 0

    now = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    signal = _make_signal()

    # First order should pass min gap
    engine.timer._last_order_time = None
    plan = engine.process_signal(signal, now, equity=10000)
    assert plan is not None

    # Record order, then immediately try again => should fail min gap
    engine.record_order_executed(now)
    now2 = now + timedelta(seconds=10)
    plan2 = engine.process_signal(signal, now2, equity=10000)
    assert plan2 is None

    # After 16 min, should pass again
    now3 = now + timedelta(minutes=16)
    plan3 = engine.process_signal(signal, now3, equity=10000)
    assert plan3 is not None


def test_engine_gate_risk_and_hygiene():
    config = StealthConfig(seed=42, no_trade_day_prob=0.0, daily_cap_range=(10, 10))
    engine = StealthExecutionEngine(config=config)
    engine.session_sim._is_no_trade_day = False
    engine.session_sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()
    engine.timer._last_order_time = None

    now = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    signal = _make_signal()
    plan = engine.process_signal(signal, now, equity=10000)
    assert plan is not None
    # Check risk params present
    assert "risk_pct" in plan
    assert 0.001 <= plan["risk_pct"] <= 0.05
    assert "sl_tp_profile" in plan
    assert "magic" in plan
    assert plan["magic"] not in range(0, 101)
    assert not (70000000 <= plan["magic"] <= 89000000)
    assert "comment" in plan
    assert "api_jitter_ms" in plan
    assert 50 <= plan["api_jitter_ms"] <= 350
    assert "delay_sec" in plan
    assert plan["delay_sec"] >= 2.5


def test_engine_disabled_passthrough():
    config = StealthConfig(seed=42, enabled=False)
    engine = StealthExecutionEngine(config=config)
    now = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    signal = _make_signal()
    plan = engine.process_signal(signal, now, equity=10000)
    assert plan is not None
    assert plan["delay_sec"] == 0.0


def test_engine_manage_position():
    config = StealthConfig(seed=42)
    engine = StealthExecutionEngine(config=config)
    now = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    position = {
        "ticket": 123,
        "id": 123,
        "entry_price": 2000,
        "current_price": 2008,  # +1.6R
        "stop_price": 1995,
        "tp_price": 2010,
        "side": "long",
        "volume": 0.10,
    }
    actions = engine.manage_position(position, now)
    # Should have trailing at least, with delay and jitter
    for act in actions:
        assert "delay_sec" in act
        assert "api_jitter_ms" in act
        assert act["delay_sec"] >= 1.0


def test_engine_record_order():
    config = StealthConfig(seed=42, daily_cap_range=(5, 5))
    engine = StealthExecutionEngine(config=config)
    now = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    engine.session_sim._current_day = now.date()
    engine.session_sim._daily_cap = 5
    engine.session_sim._orders_today = 0
    engine.timer._last_order_time = None

    assert engine.session_sim.get_orders_today() == 0
    engine.record_order_executed(now)
    assert engine.session_sim.get_orders_today() == 1
    assert engine.timer._last_order_time == now


def test_engine_seed_reproducibility():
    config1 = StealthConfig(seed=12345, no_trade_day_prob=0.0, daily_cap_range=(10, 10))
    config2 = StealthConfig(seed=12345, no_trade_day_prob=0.0, daily_cap_range=(10, 10))
    engine1 = StealthExecutionEngine(config=config1)
    engine2 = StealthExecutionEngine(config=config2)
    now = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    # Force same daily state
    for eng in (engine1, engine2):
        eng.session_sim._is_no_trade_day = False
        eng.session_sim._current_day = now.date()
        eng.timer._last_order_time = None

    signal = _make_signal()
    plan1 = engine1.process_signal(signal, now, equity=10000)
    plan2 = engine2.process_signal(signal, now, equity=10000)
    # With same seed, delays and risk should be same
    assert plan1["delay_sec"] == plan2["delay_sec"]
    assert plan1["risk_pct"] == plan2["risk_pct"]
    assert plan1["magic"] == plan2["magic"]
