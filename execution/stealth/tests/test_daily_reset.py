"""Tests for daily reset window 00:00-00:13 UTC+4 and session simulator ET lifecycle."""

from datetime import datetime, timezone, timedelta

import pytest

from execution.stealth.session_simulator import SessionSimulator
from execution.stealth.humanized_risk_manager import HumanizedRiskManager
from execution.stealth.config import StealthConfig


def test_reset_window_utc4():
    config = StealthConfig(seed=42, challenge_daily_reset_window_utc4=("00:00", "00:13"), challenge_daily_reset_offset_hours=4)
    rm = HumanizedRiskManager(seed=42, config=config)

    # Reset window 00:00-00:13 UTC+4 = 20:00-20:13 UTC
    # Inside
    inside = datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)
    assert rm._is_in_reset_window(inside) is True

    inside2 = datetime(2026, 8, 20, 20, 12, tzinfo=timezone.utc)
    assert rm._is_in_reset_window(inside2) is True

    # Outside
    outside = datetime(2026, 8, 20, 20, 13, tzinfo=timezone.utc)
    assert rm._is_in_reset_window(outside) is False

    outside2 = datetime(2026, 8, 20, 19, 59, tzinfo=timezone.utc)
    assert rm._is_in_reset_window(outside2) is False


def test_session_simulator_et_windows():
    config = StealthConfig(seed=42, challenge_daily_cap=2)
    sim = SessionSimulator(seed=42, config=config, use_et=True)

    # Force no-trade off
    sim._is_no_trade_day = False
    sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()

    # 09:20 ET before range (09:30) -> not in trading session
    early = datetime(2026, 8, 24, 9, 20, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(early) is False

    # 09:35 ET inside range 09:30-09:45 -> in session
    in_range = datetime(2026, 8, 24, 9, 35, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(in_range) is True
    assert sim.is_in_range_window(in_range) is True

    # 10:00 ET inside entry 09:45-10:30 -> in session and entry window
    in_entry = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(in_entry) is True
    assert sim.is_in_entry_window(in_entry) is True

    # 10:45 ET after entry -> not in session
    after = datetime(2026, 8, 24, 10, 45, tzinfo=timezone.utc)
    assert sim.is_in_trading_session(after) is False
    assert sim.is_in_entry_window(after) is False

    # 15:30 ET close all
    close_time = datetime(2026, 8, 24, 15, 30, tzinfo=timezone.utc)
    assert sim.should_close_all(close_time) is True


def test_tab_lifecycle():
    config = StealthConfig(seed=42)
    sim = SessionSimulator(seed=42, config=config, use_et=True)

    # Tab open 9:20-9:28 random
    open_h, open_m = sim.get_tab_open_time()
    assert 9 <= open_h <= 9
    assert 20 <= open_m <= 28

    # Wind-down 10:30-11:00 random
    wd_h, wd_m = sim.get_wind_down_time()
    assert 10 <= wd_h <= 11
    if wd_h == 10:
        assert 30 <= wd_m <= 59
    else:
        assert 0 <= wd_m <= 0

    # Check wind-down detection
    wd_time = datetime(2026, 8, 24, 10, 45, tzinfo=timezone.utc)
    sim._et_wind_down_start_min = 10 * 60 + 30
    sim._et_wind_down_end_min = 11 * 60
    assert sim.is_in_wind_down(wd_time) is True


def test_min_5_trading_days():
    config = StealthConfig(seed=42, challenge_min_trading_days=5)
    sim = SessionSimulator(seed=42, config=config, use_et=True)
    sim._is_no_trade_day = False

    # Simulate 3 trading days
    for day in range(1, 4):
        dt = datetime(2026, 8, day, 10, 0, tzinfo=timezone.utc)
        sim.force_new_day(dt)
        sim.record_order(dt)

    assert sim.get_trading_days_count() == 3
    assert sim.needs_more_trading_days() is True

    # Add 2 more
    for day in range(4, 6):
        dt = datetime(2026, 8, day, 10, 0, tzinfo=timezone.utc)
        sim.force_new_day(dt)
        sim.record_order(dt)

    assert sim.get_trading_days_count() == 5
    assert sim.needs_more_trading_days() is False


def test_daily_cap_2_for_challenge():
    config = StealthConfig(seed=42, challenge_daily_cap=2)
    sim = SessionSimulator(seed=42, config=config, use_et=True)
    sim._is_no_trade_day = False
    sim._current_day = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc).date()
    sim._daily_cap = 2
    sim._orders_today = 0

    dt = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    assert sim.can_open_new_order(dt) is True
    sim.record_order(dt)
    assert sim.can_open_new_order(dt) is True
    sim.record_order(dt)
    assert sim.get_orders_today() == 2
    assert sim.can_open_new_order(dt) is False


def test_holidays_off():
    config = StealthConfig(seed=42, market_holidays=["2026-08-24"])
    sim = SessionSimulator(seed=42, config=config, use_et=False)
    sim._is_no_trade_day = False

    holiday = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    assert sim.is_holiday(holiday) is True
    assert sim.is_in_trading_session(holiday) is False
    assert sim.can_open_new_order(holiday) is False
