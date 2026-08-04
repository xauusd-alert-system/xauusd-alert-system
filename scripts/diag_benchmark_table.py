"""Diagnostic: dump per-asset benchmark table aggregates (mean/min/max per metric).

Reads logs/backtest_<asset>.csv and prints, for each asset, the row for the
benchmarks.md Phase-0 baseline tables: avg value per metric plus min..max range.
"""
from __future__ import annotations

import os

import pandas as pd

ASSETS = ["XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"]

# columns to show, in table order
COLS = [
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


def _fmt(v: float) -> str:
    if abs(v) < 100:
        return f"{v:.2f}"
    return f"{v:,.0f}"


def main() -> None:
    for asset in ASSETS:
        path = os.path.join("logs", f"backtest_{asset.lower()}.csv")
        if not os.path.exists(path):
            print(f"\n=== {asset} MISSING {path}")
            continue
        df = pd.read_csv(path)
        n_folds = len(df)
        n_trades_total = int(df["n_trades"].sum())
        print(f"\n=== {asset}: {n_folds} folds, {n_trades_total:,} trades ===")
        for col in COLS:
            mean = df[col].mean()
            lo = df[col].min()
            hi = df[col].max()
            print(f"  {col:>26} mean={_fmt(mean):>12}  min={_fmt(lo):>12}  max={_fmt(hi):>12}")
        pos_folds = int((df["total_pnl"] > 0).sum())
        print(f"  {'positive_pnl_folds':>26} = {pos_folds}/{n_folds}")


if __name__ == "__main__":
    main()
