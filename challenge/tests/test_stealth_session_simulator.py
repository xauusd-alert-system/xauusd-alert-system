"""Tests for SessionSimulator — weekend, holidays, session phases, daily cap."""
import pytest
from datetime import datetime, date, time, timedelta, timezone

from challenge.stealth.session_simulator import SessionSimulator

ET = timezone(timedelta(hours=-4))


def _et(d, h, mi):
    """Make an ET datetime."""
    return datetime(2026, d.month, d.day, h, mi, tzinfo=ET)


class TestWeekendAndHolidays:
    def test_weekend_saturday(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_weekend(date(2026, 8, 29)) is True  # Saturday

    def test_weekend_sunday(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_weekend(date(2026, 8, 30)) is True  # Sunday

    def test_not_weekend_monday(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_weekend(date(2026, 8, 31)) is False  # Monday

    def test_market_holiday(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_market_holiday(date(2026, 1, 1)) is True  # New Year's

    def test_not_market_holiday(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_market_holiday(date(2026, 8, 25)) is False

    def test_tradeable_day(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_tradeable_day(date(2026, 8, 25)) is True  # Tuesday

    def test_not_tradeable_weekend(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_tradeable_day(date(2026, 8, 29)) is False  # Saturday

    def test_not_tradeable_holiday(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_tradeable_day(date(2026, 1, 1)) is False  # New Year's


class TestSessionPhases:
    def test_in_range_phase(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 9, 35)  # 9:35 ET
        assert ss.is_in_range_phase(now) is True

    def test_not_in_range_before(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 9, 29)
        assert ss.is_in_range_phase(now) is False

    def test_not_in_range_after(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 9, 45)
        assert ss.is_in_range_phase(now) is False

    def test_in_entry_phase(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 10, 0)
        assert ss.is_in_entry_phase(now) is True

    def test_not_in_entry_during_range(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 9, 35)
        assert ss.is_in_entry_phase(now) is False

    def test_not_in_entry_after(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 10, 30)
        assert ss.is_in_entry_phase(now) is False

    def test_in_trading_window(self):
        ss = SessionSimulator(seed=42)
        assert ss.is_in_trading_window(datetime(2026, 8, 25, 9, 35)) is True
        assert ss.is_in_trading_window(datetime(2026, 8, 25, 10, 0)) is True
        assert ss.is_in_trading_window(datetime(2026, 8, 25, 11, 0)) is False

    def test_should_close_all(self):
        ss = SessionSimulator(seed=42)
        assert ss.should_close_all(datetime(2026, 8, 25, 15, 30)) is True
        assert ss.should_close_all(datetime(2026, 8, 25, 15, 29)) is False

    def test_should_close_all_after(self):
        ss = SessionSimulator(seed=42)
        assert ss.should_close_all(datetime(2026, 8, 25, 16, 0)) is True


class TestDailyCap:
    def test_can_trade_within_cap(self):
        ss = SessionSimulator(seed=42)
        # Use a specific date that's a weekday and not holiday
        now = datetime(2026, 8, 25, 10, 0)  # Tuesday
        ss._today = date(2026, 8, 25)
        ss._skip_today = False
        ss._trades_today = 0
        assert ss.can_trade_now(now) is True

    def test_can_trade_at_cap(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 10, 0)
        ss._today = date(2026, 8, 25)
        ss._skip_today = False
        ss._trades_today = 2
        assert ss.can_trade_now(now) is False

    def test_record_trade(self):
        ss = SessionSimulator(seed=42)
        ss._today = date(2026, 8, 25)
        assert ss.trades_today == 0
        ss.record_trade()
        assert ss.trades_today == 1

    def test_mark_positions_closed(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 10, 0)
        ss._today = date(2026, 8, 25)
        ss._skip_today = False
        assert ss.can_trade_now(now) is True
        ss.mark_positions_closed()
        assert ss.can_trade_now(now) is False


class TestSkipDay:
    def test_skip_day_probabilistic(self):
        # With seed that gives skip
        ss = SessionSimulator(seed=0)
        ss._today = None
        # Force a specific date
        now = datetime(2026, 8, 25, 10, 0)
        ss._is_active_day(now.date())
        # Some seeds will skip, some won't — just verify it doesn't crash

    def test_no_skip_when_seed_avoids(self):
        ss = SessionSimulator(seed=42, cfg={"skip_day_chance": 0.0})
        now = datetime(2026, 8, 25, 10, 0)
        # With 0% skip chance, should always be active
        assert ss._is_active_day(now.date()) is True


class TestTradingDaysCount:
    def test_count_increases_on_new_day(self):
        ss = SessionSimulator(seed=42)
        ss._today = None
        ss.new_day(date(2026, 8, 25))
        ss.record_trade()
        ss.new_day(date(2026, 8, 26))
        assert ss.trading_days_count == 1

    def test_needs_more_trading_days(self):
        ss = SessionSimulator(seed=42, cfg={"min_trading_days": 5})
        assert ss.needs_more_trading_days() is True
        ss._trading_days_count = 5
        assert ss.needs_more_trading_days() is False


class TestTabOpenTime:
    def test_tab_open_in_range(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 9, 0)
        t = ss.get_tab_open_time(now)
        if t is not None:
            assert 9 * 60 + 20 <= t.hour * 60 + t.minute <= 9 * 60 + 28

    def test_tab_open_none_on_weekend(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 29, 9, 0)  # Saturday
        t = ss.get_tab_open_time(now)
        assert t is None

    def test_wind_down_in_range(self):
        ss = SessionSimulator(seed=42)
        now = datetime(2026, 8, 25, 10, 30)
        t = ss.get_wind_down_time(now)
        assert t is not None
        assert 10 * 60 + 30 <= t.hour * 60 + t.minute <= 11 * 60


class TestMT5Mode:
    def test_mt5_mode(self):
        ss = SessionSimulator(seed=42, cfg={"use_et": False})
        assert ss.use_et is False
