"""Tests for HumanizedRiskManager — sizing, limits, reset, daily/overall."""
import pytest
from datetime import datetime, timedelta

from challenge.stealth.humanized_risk_manager import HumanizedRiskManager


def _utc4(h, mi):
    """Make a naive UTC+4 datetime."""
    return datetime(2026, 8, 25, h, mi)


class TestDailyReset:
    def test_is_in_reset_window(self):
        # 20:00 UTC = 00:00 UTC+4
        utc = datetime(2026, 8, 25, 20, 5)
        assert HumanizedRiskManager._is_in_reset_window_utc(utc) is True

    def test_not_in_reset_window(self):
        utc = datetime(2026, 8, 25, 15, 0)
        assert HumanizedRiskManager._is_in_reset_window_utc(utc) is False

    def test_reset_window_boundary_start(self):
        utc = datetime(2026, 8, 25, 20, 0)
        assert HumanizedRiskManager._is_in_reset_window_utc(utc) is True

    def test_reset_window_boundary_end(self):
        utc = datetime(2026, 8, 25, 20, 13)
        assert HumanizedRiskManager._is_in_reset_window_utc(utc) is True

    def test_reset_window_just_after(self):
        utc = datetime(2026, 8, 25, 20, 14)
        assert HumanizedRiskManager._is_in_reset_window_utc(utc) is False

    def test_reset_updates_balance_at_day_start(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        # Feed equity
        rm.update_floating_pnl(-5.0, 995.0, _utc4(18, 0))
        assert rm.balance_at_day_start == 1000.0
        # Enter reset window (00:00 UTC+4 = 20:00 UTC)
        rm.update_floating_pnl(-20.0, 980.0, _utc4(0, 5))
        # After reset, balance_at_day_start should be ~980
        assert rm.balance_at_day_start == 980.0
        # daily_pnl should be near 0 or -20 (depending on timing)
        dp = rm.daily_pnl()
        assert dp >= -25.0  # should have reset close to 0

    def test_daily_pnl_calculation(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(-10.0, 990.0, _utc4(10, 0))
        rm.update_closed_pnl(-3.0)
        assert rm.daily_pnl() == pytest.approx(-13.0)


class TestCanTrade:
    def test_can_trade_initially(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(0, 1000.0, _utc4(10, 0))
        ok, reason = rm.can_trade()
        assert ok is True

    def test_daily_limit_blocks(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(-31.0, 969.0, _utc4(10, 0))
        ok, reason = rm.can_trade()
        assert ok is False
        assert "daily" in reason

    def test_overall_buffer_blocks(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(-50.0, 905.0, _utc4(10, 0))
        rm.update_closed_pnl(-41.0)  # total = -91
        ok, reason = rm.can_trade()
        assert ok is False
        assert "overall" in reason

    def test_can_trade_with_overrides(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        ok, _ = rm.can_trade(daily_pnl=-5.0, overall_pnl=-10.0)
        assert ok is True

    def test_zero_equity_blocks(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(0, 0.0, _utc4(10, 0))
        ok, _ = rm.can_trade()
        assert ok is False


class TestForceClose:
    def test_force_close_on_daily_limit(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(-31.0, 969.0, _utc4(10, 0))
        assert rm.should_force_close() is True

    def test_force_close_on_overall_buffer(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(-50.0, 905.0, _utc4(10, 0))
        rm.update_closed_pnl(-41.0)
        assert rm.should_force_close() is True

    def test_no_force_close_within_limits(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(-10.0, 990.0, _utc4(10, 0))
        assert rm.should_force_close() is False


class TestPositionSizing:
    def test_position_size_basic(self):
        rm = HumanizedRiskManager(start_balance=1000.0, risk_base_pct=0.01, seed=42)
        rm.update_floating_pnl(0, 1000.0, _utc4(10, 0))
        # $10 risk / $0.50 stop = 20 shares (with jitter)
        shares = rm.position_size(0.50, 100.0)
        assert 10 <= shares <= 30  # Allow for jitter

    def test_position_size_respects_leverage(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(0, 1000.0, _utc4(10, 0))
        # $1000 * 5 / $500 = 10 shares max
        shares = rm.position_size(0.10, 500.0)
        assert shares <= 10

    def test_position_size_zero_on_bad_input(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(0, 1000.0, _utc4(10, 0))
        assert rm.position_size(0, 100.0) == 0
        assert rm.position_size(0.50, 0) == 0

    def test_notional_ok_within_leverage(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(0, 1000.0, _utc4(10, 0))
        # 10 shares * $100 = $1000 < $5000 buying power
        assert rm.notional_ok(10, 100.0) is True

    def test_notional_exceeds_leverage(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(0, 1000.0, _utc4(10, 0))
        # 60 shares * $100 = $6000 > $5000
        assert rm.notional_ok(60, 100.0) is False


class TestSLTPProfile:
    def test_select_profile_returns_valid(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        sl_m, tp_m = rm.select_sl_tp_profile()
        assert sl_m > 0
        assert tp_m > sl_m  # TP always wider than SL

    def test_profile_no_repeat_tendency(self):
        rm = HumanizedRiskManager(seed=42)
        seen = set()
        for _ in range(50):
            sl_m, tp_m = rm.select_sl_tp_profile()
            seen.add((round(sl_m, 2), round(tp_m, 2)))
        # With 70% no-repeat, should see multiple different profiles
        assert len(seen) >= 3


class TestOverallPnl:
    def test_overall_pnl(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(-5.0, 995.0, _utc4(10, 0))
        assert rm.overall_pnl() == pytest.approx(-5.0)

    def test_overall_pnl_with_closed(self):
        rm = HumanizedRiskManager(start_balance=1000.0, seed=42)
        rm.update_floating_pnl(-5.0, 995.0, _utc4(10, 0))
        rm.update_closed_pnl(-3.0)
        # overall = equity - start = 995 - 1000 = -5 (closed_pnl already in equity)
        assert rm.overall_pnl() == pytest.approx(-5.0)


class TestRiskUsd:
    def test_risk_usd_in_range(self):
        rm = HumanizedRiskManager(start_balance=1000.0, risk_base_pct=0.01, seed=42)
        rm.update_floating_pnl(0, 1000.0, _utc4(10, 0))
        risks = [rm.risk_usd() for _ in range(100)]
        # Base is $10, with jitter 0.65-1.35% = $6.50-$13.50
        # With 5% OOB (1.5-2x) = $15-$27
        for r in risks:
            assert 0 < r < 30  # reasonable bounds

    def test_risk_usd_changes_with_equity(self):
        rm = HumanizedRiskManager(start_balance=1000.0, risk_base_pct=0.01, seed=42)
        rm.update_floating_pnl(0, 1000.0, _utc4(10, 0))
        avg1 = sum(rm.risk_usd() for _ in range(20)) / 20
        rm.update_floating_pnl(0, 800.0, _utc4(10, 0))
        avg2 = sum(rm.risk_usd() for _ in range(20)) / 20
        assert avg2 < avg1
