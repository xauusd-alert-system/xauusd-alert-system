"""
Tests for Monte Carlo Simulation & Stress Testing Engine.
"""
import numpy as np
import pytest

from backtest.monte_carlo import MonteCarloSimulator


def test_monte_carlo_positive_edge():
    np.random.seed(42)
    # Positive edge: 60% win ($150), 40% loss (-$100)
    pnls = [150.0 if np.random.rand() < 0.6 else -100.0 for _ in range(200)]
    mc = MonteCarloSimulator(pnls, initial_balance=10000.0, n_simulations=500, horizon_trades=50)
    res = mc.run_simulation()

    assert res["n_simulations"] == 500
    assert res["horizon_trades"] == 50
    assert res["mean_ending_equity"] > 10000.0
    assert res["profit_probability_pct"] > 80.0
    assert res["prob_of_ruin_pct"] == 0.0
    assert res["max_drawdown_median_pct"] > 0.0


def test_monte_carlo_empty():
    mc = MonteCarloSimulator([], initial_balance=5000.0)
    res = mc.run_simulation()
    assert res["median_ending_equity"] == 5000.0
    assert res["var_95_usd"] == 0.0
