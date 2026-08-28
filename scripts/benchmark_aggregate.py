"""Aggregate per-fold walk-forward metrics for the docs/benchmarks.md baseline tables.

Reads logs/backtest_<asset>.csv (written by scripts/run_backtest.py) and prints, for
each asset, the number of folds, total trades, and the per-fold mean of each metric
column, plus the net sum of total_pnl.

Usage:
    python scripts/benchmark_aggregate.py [asset ...]  # default: all five assets
"""

import os
import sys

import pandas as pd

METRIC_COLS = [
    "n_trades",
    "win_rate",
    "profit_factor",
    "sharpe_ratio",
    "sortino_ratio",
    "expectancy",
    "max_drawdown",
    "total_pnl",
    "max_consecutive_losses",
]

ASSETS = ["XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"]


def main() -> None:
    assets = sys.argv[1:] if len(sys.argv) > 1 else ASSETS
    for asset in assets:
        path = os.path.join("logs", f"backtest_{asset.lower()}.csv")
        if not os.path.exists(path):
            print(f"NOTE: {path} not found, skipping {asset}.")
            continue
        df = pd.read_csv(path)
        missing = [c for c in METRIC_COLS if c not in df.columns]
        if missing:
            print(f"NOTE: {path} missing columns {missing}, skipping {asset}.")
            continue
        subset = df[METRIC_COLS]
        means = subset.mean().round(2)
        print(f"== {asset} == folds={len(df)} total_trades={int(subset['n_trades'].sum())}")
        print("  " + means.to_dict().__repr__())
        print(f"  net_total_pnl_sum={subset['total_pnl'].sum().round(2)}")


if __name__ == "__main__":
    main()
