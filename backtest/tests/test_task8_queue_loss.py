import pandas as pd
import pytest

from backtest.portfolio import (
    calculate_queue_loss,
    daily_r_matrix,
    rank_concurrent_signals,
    strategy_correlation,
)


def test_task8_rank_concurrent_signals():
    """Unit test Task 8: signals firing simultaneously are ranked by confidence/EV."""
    signals = [
        {"asset": "XAUUSD", "confidence": 0.72, "ev": 0.15},
        {"asset": "EURUSD", "confidence": 0.88, "ev": 0.25},
        {"asset": "BTCUSD", "confidence": 0.65, "ev": 0.10},
    ]

    ranked_by_conf = rank_concurrent_signals(signals, ranking_metric="confidence")
    assert ranked_by_conf[0]["asset"] == "EURUSD"
    assert ranked_by_conf[1]["asset"] == "XAUUSD"
    assert ranked_by_conf[2]["asset"] == "BTCUSD"

    ranked_by_ev = rank_concurrent_signals(signals, ranking_metric="ev")
    assert ranked_by_ev[0]["asset"] == "EURUSD"


def test_task8_calculate_queue_loss_critical_flag():
    """Unit test Task 8: flags critical queue loss when rejected signals have higher E[R]."""
    taken = [{"net_r": 0.05}, {"net_r": 0.02}, {"net_r": -0.04}]  # E[R] = 0.01
    rejected = [{"net_r": 0.15}, {"net_r": 0.20}, {"net_r": 0.10}]  # E[R] = 0.15 (higher!)

    rep = calculate_queue_loss(taken, rejected)
    assert rep["e_r_taken"] == pytest.approx(0.01)
    assert rep["e_r_rejected"] == pytest.approx(0.15)
    assert rep["is_critical"] is True
    assert rep["warning"] is not None
    assert "CRITICAL QUEUE LOSS" in rep["warning"]


def test_task8_portfolio_uses_strategy_daily_r_correlations():
    """Unit test Task 8: backtest/portfolio.py calculates correlation of strategy daily R sums,
    NOT asset price correlation."""
    # Strategy A and Strategy B have identical spot price underlying, but different strategy daily R
    df_a = pd.DataFrame({"entry_ts": [1700000000, 1700086400], "net_r": [0.5, -0.3]})
    df_b = pd.DataFrame({"entry_ts": [1700000000, 1700086400], "net_r": [-0.5, 0.3]})

    daily = daily_r_matrix({"A": df_a, "B": df_b})
    corr = strategy_correlation(daily)

    # Strategy returns are perfectly negatively correlated (-1.0)
    assert corr.loc["A", "B"] == pytest.approx(-1.0)
