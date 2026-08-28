"""Aggregate per-asset walk-forward CSVs into concise summary rows for benchmarks.md.

Reads logs/backtest_<asset>.csv for each asset and prints:
  asset, folds, total_trades, trades_only_folds, mean_win_rate,
  mean_expectancy, sum_total_pnl, positive_pnl_folds, min_win_rate, max_win_rate
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ASSETS = ["XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"]

HEADER = (
    f"{'asset':<7} {'folds':>5} {'trades':>7} {'wr_mean':>8} "
    f"{'wr_min':>7} {'wr_max':>7} {'exp_mean':>9} {'pnl_sum':>11} {'pos_folds':>9}"
)


def main() -> None:
    print(HEADER)
    for asset in ASSETS:
        path = f"logs/backtest_{asset.lower()}.csv"
        if not os.path.exists(path):
            print(f"{asset:<7}  <missing {path}>")
            continue
        df = pd.read_csv(path)
        trades_only = df[df["n_trades"] > 0]
        n_folds = len(df)
        total_trades = int(df["n_trades"].sum())
        wr_mean = float(df["win_rate"].mean()) if n_folds else float("nan")
        wr_min = float(df["win_rate"].min()) if n_folds and len(df) else float("nan")
        wr_max = float(df["win_rate"].max()) if n_folds else float("nan")
        exp_mean = float(df["expectancy"].mean()) if n_folds else float("nan")
        pnl_sum = float(df["total_pnl"].sum()) if n_folds else float("nan")
        pos_folds = int((df["total_pnl"] > 0).sum())
        print(
            f"{asset:<7} {n_folds:>5} {total_trades:>7} {wr_mean:>8.2f} "
            f"{wr_min:>7.2f} {wr_max:>7.2f} {exp_mean:>9.3f} {pnl_sum:>11.2f} {pos_folds:>9}"
        )


if __name__ == "__main__":
    main()
