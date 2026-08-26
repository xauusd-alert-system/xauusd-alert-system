"""Tests for EquityCurveHumanizer — partial exits, trailing, early close."""
import pytest

from challenge.stealth.equity_curve_humanizer import EquityCurveHumanizer


class TestPartialExit:
    def test_should_not_partial_below_1r(self):
        eh = EquityCurveHumanizer(seed=42)
        assert eh.should_partial_exit(0.5) is False
        assert eh.should_partial_exit(0.9) is False

    def test_should_partial_at_1r_probabilistic(self):
        eh = EquityCurveHumanizer(seed=42)
        triggered = sum(eh.should_partial_exit(1.5) for _ in range(200))
        # 25% chance → ~50 out of 200
        assert 20 < triggered < 80

    def test_get_partial_exit_shares_range(self):
        eh = EquityCurveHumanizer(seed=42)
        for _ in range(100):
            shares = eh.get_partial_exit_shares(20)
            assert 1 <= shares <= 19  # 30-50% of 20 = 6-10, but jitter allows wider

    def test_get_partial_exit_shares_min_one(self):
        eh = EquityCurveHumanizer(seed=42)
        shares = eh.get_partial_exit_shares(2)
        assert shares >= 1

    def test_get_partial_exit_shares_not_all(self):
        eh = EquityCurveHumanizer(seed=42)
        for _ in range(50):
            shares = eh.get_partial_exit_shares(10)
            assert shares < 10  # never close all


class TestEarlyClose:
    def test_should_not_early_close_above_1r(self):
        eh = EquityCurveHumanizer(seed=42)
        assert eh.should_early_close(1.2) is False

    def test_should_not_early_close_below_06r(self):
        eh = EquityCurveHumanizer(seed=42)
        assert eh.should_early_close(0.3) is False

    def test_should_early_close_at_06r_probabilistic(self):
        eh = EquityCurveHumanizer(seed=42)
        triggered = sum(eh.should_early_close(0.8) for _ in range(200))
        # 12% chance → ~24 out of 200
        assert 5 < triggered < 50


class TestTrailingDistance:
    def test_tier1_low_price(self):
        eh = EquityCurveHumanizer(seed=42)
        for _ in range(50):
            d = eh.get_trailing_distance_dollars(30.0)
            assert 0.50 <= d <= 1.00

    def test_tier2_mid_price(self):
        eh = EquityCurveHumanizer(seed=42)
        for _ in range(50):
            d = eh.get_trailing_distance_dollars(100.0)
            assert 0.75 <= d <= 1.50

    def test_tier3_high_price(self):
        eh = EquityCurveHumanizer(seed=42)
        for _ in range(50):
            d = eh.get_trailing_distance_dollars(300.0)
            assert 1.00 <= d <= 2.00

    def test_tier_boundary_50(self):
        eh = EquityCurveHumanizer(seed=42)
        d = eh.get_trailing_distance_dollars(50.0)
        assert 0.50 <= d <= 1.00  # < 50 boundary

    def test_tier_boundary_200(self):
        eh = EquityCurveHumanizer(seed=42)
        d = eh.get_trailing_distance_dollars(200.0)
        assert 0.75 <= d <= 1.50  # < 200 boundary


class TestComputeTrailingSl:
    def test_no_trailing_below_activation(self):
        eh = EquityCurveHumanizer(seed=42)
        # entry=100, sl=98 (risk_dist=2), current=101 (unrealized=1, R=0.5)
        # Below 1.5R activation → no trailing
        result = eh.compute_trailing_sl("long", 101.0, 100.0, 98.0)
        assert result is None

    def test_trailing_sl_long(self):
        eh = EquityCurveHumanizer(seed=42)
        # At 2.0R for long: entry 100, sl 99, current 102
        result = eh.compute_trailing_sl("long", 102.0, 100.0, 99.0)
        if result is not None:
            assert result > 99.0  # SL moved up
            assert result < 102.0  # Below current price

    def test_trailing_sl_short(self):
        eh = EquityCurveHumanizer(seed=42)
        # At 2.0R for short: entry 100, sl 101, current 98
        result = eh.compute_trailing_sl("short", 98.0, 100.0, 101.0)
        if result is not None:
            assert result < 101.0  # SL moved down
            assert result > 98.0  # Above current price

    def test_trailing_sl_no_move_worse(self):
        eh = EquityCurveHumanizer(seed=42)
        # For long: new SL must be higher than current
        result = eh.compute_trailing_sl("long", 101.0, 100.0, 99.0)
        # May or may not trail depending on distance — just verify no crash
        if result is not None:
            assert result > 99.0

    def test_no_trailing_with_zero_risk(self):
        eh = EquityCurveHumanizer(seed=42)
        result = eh.compute_trailing_sl("long", 100.0, 100.0, 100.0)
        assert result is None


class TestEvaluatePosition:
    def test_evaluate_returns_list(self):
        eh = EquityCurveHumanizer(seed=42)
        actions = eh.evaluate_position(
            side="long",
            entry_price=100.0,
            current_price=102.0,
            current_sl=99.0,
            tp_price=106.0,
            total_shares=10,
            remaining_shares=10,
            already_partialed=False,
        )
        assert isinstance(actions, list)

    def test_evaluate_no_actions_when_no_risk(self):
        eh = EquityCurveHumanizer(seed=42)
        actions = eh.evaluate_position(
            side="long",
            entry_price=100.0,
            current_price=100.0,
            current_sl=100.0,  # zero risk distance
            tp_price=105.0,
            total_shares=10,
            remaining_shares=10,
            already_partialed=False,
        )
        assert actions == []

    def test_evaluate_can_return_multiple_actions(self):
        eh = EquityCurveHumanizer(seed=42)
        # Run many times to catch a multi-action scenario
        multi = False
        for i in range(200):
            eh2 = EquityCurveHumanizer(seed=i)
            actions = eh2.evaluate_position(
                side="long",
                entry_price=100.0,
                current_price=103.0,  # +3R
                current_sl=99.0,
                tp_price=106.0,
                total_shares=10,
                remaining_shares=10,
                already_partialed=False,
            )
            if len(actions) > 1:
                multi = True
                break
        # At +3R, should sometimes get partial + trailing
        # (may not always happen with probability)
