"""
Monte Carlo Simulation & Portfolio Stress Testing Engine.
Computes Value at Risk (VaR), Conditional Value at Risk (CVaR/Expected Shortfall),
Risk of Ruin, and Drawdown Distributions over thousands of resampled equity paths.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional


class MonteCarloSimulator:
    """
    Simulates stochastic equity paths from trade returns or price returns.
    """

    def __init__(
        self,
        trade_pnls: List[float] | np.ndarray | pd.Series,
        initial_balance: float = 10000.0,
        n_simulations: int = 1000,
        horizon_trades: int = 100,
        random_seed: int = 42,
    ):
        self.trade_pnls = np.asarray(trade_pnls, dtype=float)
        self.initial_balance = float(initial_balance)
        self.n_simulations = int(n_simulations)
        self.horizon_trades = int(horizon_trades)
        self.random_seed = random_seed

    def run_simulation(self) -> Dict[str, Any]:
        """
        Runs Monte Carlo bootstrap resampling and computes risk metrics.
        """
        if len(self.trade_pnls) == 0:
            return self._empty_result()

        rng = np.random.default_rng(self.random_seed)
        
        # Sample with replacement: (n_simulations, horizon_trades)
        sampled_trades = rng.choice(
            self.trade_pnls,
            size=(self.n_simulations, self.horizon_trades),
            replace=True,
        )

        # Cumulative PnL paths: shape (n_simulations, horizon_trades + 1)
        cum_pnl = np.hstack([
            np.zeros((self.n_simulations, 1)),
            np.cumsum(sampled_trades, axis=1),
        ])
        equity_paths = self.initial_balance + cum_pnl

        # Calculate drawdowns for each path
        running_max = np.maximum.accumulate(equity_paths, axis=1)
        drawdowns_usd = equity_paths - running_max
        drawdowns_pct = (drawdowns_usd / np.maximum(running_max, 1e-6)) * 100.0
        
        max_drawdowns_pct = np.abs(np.min(drawdowns_pct, axis=1))
        ending_equities = equity_paths[:, -1]
        ending_pnls = ending_equities - self.initial_balance

        # Value at Risk (VaR) & CVaR (Expected Shortfall)
        var_95 = float(np.percentile(ending_pnls, 5.0))
        var_99 = float(np.percentile(ending_pnls, 1.0))
        
        # CVaR is mean of losses beyond VaR
        tail_95 = ending_pnls[ending_pnls <= var_95]
        tail_99 = ending_pnls[ending_pnls <= var_99]
        cvar_95 = float(np.mean(tail_95)) if len(tail_95) > 0 else var_95
        cvar_99 = float(np.mean(tail_99)) if len(tail_99) > 0 else var_99

        # Ruin probability (ruin = equity drops by >= 50%)
        ruin_threshold = self.initial_balance * 0.5
        min_equities = np.min(equity_paths, axis=1)
        ruin_count = np.sum(min_equities <= ruin_threshold)
        prob_of_ruin_pct = float((ruin_count / self.n_simulations) * 100.0)

        # Drawdown percentiles
        max_dd_median = float(np.median(max_drawdowns_pct))
        max_dd_95 = float(np.percentile(max_drawdowns_pct, 95.0))
        max_dd_99 = float(np.percentile(max_drawdowns_pct, 99.0))

        # Win probability (portfolio in profit at horizon)
        profit_prob_pct = float((np.sum(ending_pnls > 0) / self.n_simulations) * 100.0)

        return {
            "n_simulations": self.n_simulations,
            "horizon_trades": self.horizon_trades,
            "initial_balance": self.initial_balance,
            "median_ending_equity": float(np.median(ending_equities)),
            "mean_ending_equity": float(np.mean(ending_equities)),
            "profit_probability_pct": profit_prob_pct,
            "var_95_usd": var_95,
            "var_99_usd": var_99,
            "cvar_95_usd": cvar_95,
            "cvar_99_usd": cvar_99,
            "max_drawdown_median_pct": max_dd_median,
            "max_drawdown_95_pct": max_dd_95,
            "max_drawdown_99_pct": max_dd_99,
            "prob_of_ruin_pct": prob_of_ruin_pct,
        }

    def _empty_result(self) -> Dict[str, Any]:
        return {
            "n_simulations": self.n_simulations,
            "horizon_trades": self.horizon_trades,
            "initial_balance": self.initial_balance,
            "median_ending_equity": self.initial_balance,
            "mean_ending_equity": self.initial_balance,
            "profit_probability_pct": 50.0,
            "var_95_usd": 0.0,
            "var_99_usd": 0.0,
            "cvar_95_usd": 0.0,
            "cvar_99_usd": 0.0,
            "max_drawdown_median_pct": 0.0,
            "max_drawdown_95_pct": 0.0,
            "max_drawdown_99_pct": 0.0,
            "prob_of_ruin_pct": 0.0,
        }
