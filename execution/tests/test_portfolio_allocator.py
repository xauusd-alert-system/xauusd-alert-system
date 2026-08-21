"""
Tests for Phase 11 Portfolio Allocator & Risk Parity.
"""
import numpy as np
import pandas as pd
import pytest

from execution.portfolio_allocator import (
    calculate_fractional_kelly,
    inverse_volatility_allocation,
    calculate_lot_size,
    hierarchical_risk_parity,
)


def test_fractional_kelly_sizing():
    # 60% win rate, 1.5 win/loss ratio -> positive edge
    f = calculate_fractional_kelly(win_rate=0.60, win_loss_ratio=1.5, fraction=0.5)
    assert 0.005 <= f <= 0.05

    # Negative edge -> returns min_risk
    f_neg = calculate_fractional_kelly(win_rate=0.30, win_loss_ratio=1.0, fraction=0.5)
    assert f_neg == 0.005


def test_inverse_volatility_allocation():
    vols = {"XAUUSD": 0.02, "BTCUSD": 0.06, "EURUSD": 0.01}
    weights = inverse_volatility_allocation(vols)
    assert np.isclose(sum(weights.values()), 1.0)
    # EURUSD should have highest weight, BTCUSD lowest
    assert weights["EURUSD"] > weights["XAUUSD"] > weights["BTCUSD"]


def test_calculate_lot_size_scaling():
    # $10,000 equity, 2% risk = $200 risk.
    # Gold: stop = $5.00, point_value = 100 -> $500 risk per 1.0 lot -> 0.40 lots
    lots = calculate_lot_size(
        account_equity=10000.0,
        risk_pct=2.0,
        stop_loss_distance=5.0,
        point_value_lot=100.0,
    )
    assert np.isclose(lots, 0.40)


def test_hierarchical_risk_parity():
    np.random.seed(42)
    returns = pd.DataFrame({
        "XAUUSD": np.random.randn(100) * 0.01,
        "EURUSD": np.random.randn(100) * 0.005,
        "BTCUSD": np.random.randn(100) * 0.03,
    })
    weights = hierarchical_risk_parity(returns)
    assert np.isclose(weights.sum(), 1.0)
    assert weights["EURUSD"] > weights["BTCUSD"]


def test_calculate_lot_size_never_rounds_up_to_minimum():
    """N7: a risk-based size below min_lot must NOT be rounded up to min_lot
    (that would exceed the intended per-trade risk). It returns 0 to signal
    'skip', matching risk_sizer.lots_for_risk's 'never round up' rule."""
    lots = calculate_lot_size(
        account_equity=100.0,      # tiny account
        risk_pct=0.25,             # 0.25% -> $0.25 risk
        stop_loss_distance=5.0,
        point_value_lot=100.0,
        min_lot=0.01,
    )
    # raw = 0.25 / (5*100) = 0.0005 < min_lot -> must NOT be clipped to 0.01
    assert lots == 0.0
