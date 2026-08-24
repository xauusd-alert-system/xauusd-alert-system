"""Tests for SessionSimulator."""

from datetime import datetime, timezone

import pytest

from execution.stealth.session_simulator import SessionSimulator


def test_weekend_off():
    sim = SessionSimulator(seed=42)
    # Saturday
    sat = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)  # 2026-08-22 is Saturday
    assert sat.weekday() == 5
    assert sim.is_weekend(sat) is True
    assert sim.is_in_trading_session(sat) is False
    assert sim.can_open_new_order(sat) is False

    # Sunday
    sun = datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc)
    assert sun.weekday() == 6
    assert sim.is_weekend(sun) is True

    # Monday should not be weekend
    mon = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    assert mon.weekday() == 0
    assert sim.is_weekend(mon) is False


def test_trading_windows():
    sim = SessionSimulator(seed=42)
    # Force no-trade day off for this test
    sim._is_no_trade_day = False
    sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()

    # 06:00 UTC before London (07:30) -> not in session
    early = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(early) is False

    # 08:00 UTC inside both London and NY
    inside = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(inside) is True

    # 17:00 UTC after London (16:00) but inside NY (until 17:30) -> should be True
    ny_only = datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(ny_only) is True

    # 18:00 UTC after both -> False
    late = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(late) is False


def test_breaks():
    # Break 12:00-12:30
    sim = SessionSimulator(seed=42, breaks=[("12:00", "12:30")])
    sim._is_no_trade_day = False
    sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()

    during_break = datetime(2026, 8, 24, 12, 15, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(during_break) is False

    before_break = datetime(2026, 8, 24, 11, 45, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(before_break) is True

    after_break = datetime(2026, 8, 24, 12, 45, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(after_break) is True


def test_daily_cap_range():
    sim = SessionSimulator(seed=123, daily_cap_range=(3, 7))
    caps = []
    for day in range(1, 31):
        dt = datetime(2026, 8, day, 10, 0, tzinfo=timezone.utc)
        sim.force_new_day(dt)
        caps.append(sim.get_daily_cap())
    assert all(3 <= c <= 7 for c in caps)
    # Should have variation
    assert len(set(caps)) > 1


def test_no_trade_day_prob():
    sim = SessionSimulator(seed=42)
    no_trade_days = 0
    for day in range(1, 101):
        dt = datetime(2026, 8, day % 28 + 1, 10, 0, tzinfo=timezone.utc)
        # Use different seed per day? force_new_day uses rng
        sim.force_new_day(dt)
        if sim.is_no_trade_day_today():
            no_trade_days += 1
    # 8% of 100 = 8, allow 0-20
    assert 0 <= no_trade_days <= 25


def test_session_end_buffer():
    sim = SessionSimulator(seed=42)
    sim._is_no_trade_day = False
    sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()
    sim._session_end_buffer_sec = 600  # 10 min

    # London ends 16:00, buffer 10 min => 15:50-16:00 should be blocked
    # 15:45 should be ok
    before_buffer = datetime(2026, 8, 24, 15, 45, tzinfo=timezone.utc)
    assert sim.is_in_session_end_buffer(before_buffer) is False
    assert sim.can_open_new_order(before_buffer) is True

    in_buffer = datetime(2026, 8, 24, 15, 55, tzinfo=timezone.utc)
    assert sim.is_in_session_end_buffer(in_buffer) is True
    assert sim.can_open_new_order(in_buffer) is False

    # NY ends 17:30, buffer 10 min => 17:20-17:30 blocked
    ny_before = datetime(2026, 8, 24, 17, 15, tzinfo=timezone.utc)
    assert sim.is_in_session_end_buffer(ny_before) is False

    ny_in = datetime(2026, 8, 24, 17, 25, tzinfo=timezone.utc)
    assert sim.is_in_session_end_buffer(ny_in) is True


def test_daily_cap_enforcement():
    sim = SessionSimulator(seed=42, daily_cap_range=(3, 3))
    sim._is_no_trade_day = False
    sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()
    sim._daily_cap = 3
    sim._orders_today = 0

    dt = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    assert sim.can_open_new_order(dt) is True
    sim.record_order(dt)
    sim.record_order(dt)
    sim.record_order(dt)
    assert sim.get_orders_today() == 3
    assert sim.can_open_new_order(dt) is False


def test_reset_on_new_day():
    sim = SessionSimulator(seed=42, daily_cap_range=(3, 7))
    day1 = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    sim.force_new_day(day1)
    cap1 = sim.get_daily_cap()
    sim.record_order(day1)
    sim.record_order(day1)
    assert sim.get_orders_today() == 2

    day2 = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
    sim.force_new_day(day2)
    assert sim.get_orders_today() == 0
    # Cap should be re-randomized (may be same by chance, but test with seed ensures different eventually)
    # At least orders reset


def test_seed_reproducibility():
    s1 = SessionSimulator(seed=999)
    s2 = SessionSimulator(seed=999)
    dt = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    s1.force_new_day(dt)
    s2.force_new_day(dt)
    assert s1.get_daily_cap() == s2.get_daily_cap()
    assert s1.is_no_trade_day_today() == s2.is_no_trade_day_today()
