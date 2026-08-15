"""
Live execution monitor for demo-enabled assets.

Reads the executed_trades SQLite table populated by
execution/mt5_trader.py (via data/trade_logger.py) and compares realized
metrics against the pre-lock backtest baselines frozen in
docs/asset_status_2026_08_15.md.

This script does NOT touch market data or the locked hold-out. It works only
with the executed-trades log.

Usage:
    python -m scripts.monitor_live_execution --asset BTCUSD
    python -m scripts.monitor_live_execution --asset BTCUSD --db-path path/to/db.sqlite
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.trade_logger import read_executed_trades


# Pre-lock BTCUSD baseline from the 2026-08-15 gate run.
BASELINE = {
    "BTCUSD": {
        "expected_wr": 79.3,
        "expected_pf": 2.08,
        "expected_pnl_per_trade": 6.02,  # 29654.4 / 4929
        "min_trades_for_alert": 10,
        "warning_pf_below": 1.5,
        "warning_wr_below": 70.0,
    }
}


def compute_live_metrics(df: pd.DataFrame) -> dict:
    """Compute execution quality metrics from executed_trades rows.

    Rows with close_time < entry_time are excluded from the metrics and
    reported as `excluded_bad_duration`. They are a logging defect, not an
    execution event, and must not silently distort WR/PF.
    """
    if df.empty:
        return {
            "n_trades": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "mean_duration_min": 0.0,
            "median_duration_min": 0.0,
            "first_entry": None,
            "last_close": None,
            "excluded_bad_duration": 0,
        }

    df = df.copy()
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
    df["entry_time"] = pd.to_numeric(df["entry_time"], errors="coerce")
    df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")

    # Exclude malformed rows where close_time < entry_time (logging defect).
    bad_mask = (df["close_time"] - df["entry_time"]) < 0
    n_bad = int(bad_mask.sum())
    if n_bad > 0:
        df = df[~bad_mask]

    n = len(df)
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    gross_profit = float(wins["pnl"].sum()) if len(wins) else 0.0
    gross_loss = float(-losses["pnl"].sum()) if len(losses) else 0.0
    pf = (gross_profit / gross_loss) if gross_loss > 0 else 999.0
    wr = 100.0 * len(wins) / n if n > 0 else 0.0

    duration_min = (df["close_time"] - df["entry_time"]) / 60.0
    valid_duration = duration_min[pd.notna(duration_min) & (duration_min >= 0)]

    return {
        "n_trades": int(n),
        "total_pnl": round(float(df["pnl"].sum()), 2),
        "avg_pnl": round(float(df["pnl"].mean()), 4) if n > 0 else 0.0,
        "win_rate": round(wr, 1),
        "profit_factor": round(pf, 2) if pf != 999.0 else 999.0,
        "mean_duration_min": round(float(valid_duration.mean()), 1) if len(valid_duration) else 0.0,
        "median_duration_min": round(float(valid_duration.median()), 1) if len(valid_duration) else 0.0,
        "first_entry": int(df["entry_time"].min()) if n else None,
        "last_close": int(df["close_time"].max()) if n else None,
        "excluded_bad_duration": n_bad,
    }


def _daily_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate executed trades by close date for the CSV output."""
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["close_date"] = pd.to_datetime(
        df["close_time"], unit="s", utc=True, errors="coerce"
    ).dt.date
    rows = []
    for date, group in df.groupby("close_date"):
        m = compute_live_metrics(group)
        m["close_date"] = str(date)
        rows.append(m)
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Monitor live execution quality from the executed_trades log.")
    parser.add_argument("--asset", required=True, help="Asset key, e.g. BTCUSD")
    parser.add_argument("--db-path", default=None,
                        help="SQLite DB with executed_trades (default: config general.db_path)")
    parser.add_argument("--out-dir", default="logs", help="Directory for the summary CSV")
    args = parser.parse_args(argv)

    cfg = load_config()
    db_path = args.db_path or cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    df = read_executed_trades(db_path, symbol=args.asset)
    metrics = compute_live_metrics(df)

    print(f"\n=== Live execution monitor: {args.asset} ===")
    print(f"DB: {db_path}")
    print(f"Closed trades (valid): {metrics['n_trades']}")
    if metrics.get("excluded_bad_duration", 0) > 0:
        print(f"⚠️  Excluded {metrics['excluded_bad_duration']} trade(s) with close_time < entry_time (logging defect).")
    print(f"Total PnL: {metrics['total_pnl']}")
    print(f"Avg PnL per trade: {metrics['avg_pnl']}")
    print(f"Win rate: {metrics['win_rate']}%")
    print(f"Profit factor: {metrics['profit_factor']}")
    print(f"Mean duration: {metrics['mean_duration_min']} min")
    print(f"Median duration: {metrics['median_duration_min']} min")

    if metrics["n_trades"] == 0:
        print("No valid closed trades yet. Check again after the first positions close.")
        return

    baseline = BASELINE.get(args.asset)
    if baseline is not None and metrics["n_trades"] >= baseline["min_trades_for_alert"]:
        warnings = []
        if metrics["profit_factor"] < baseline["warning_pf_below"]:
            warnings.append(
                f"PF {metrics['profit_factor']} < {baseline['warning_pf_below']} "
                f"(backtest PF {baseline['expected_pf']})"
            )
        if metrics["win_rate"] < baseline["warning_wr_below"]:
            warnings.append(
                f"WR {metrics['win_rate']}% < {baseline['warning_wr_below']}% "
                f"(backtest WR {baseline['expected_wr']}%)"
            )
        if warnings:
            print("\n⚠️  Divergence from backtest baseline detected:")
            for w in warnings:
                print(f"  - {w}")
            print("Review execution slippage, entry timing, and trade log completeness.")
        else:
            print("\n✔ Live metrics are within the expected range of the pre-lock baseline.")
    elif baseline is not None:
        print(f"\nNot enough valid trades for comparison yet "
              f"({metrics['n_trades']} < {baseline['min_trades_for_alert']}).")
    else:
        print("\nNo baseline configured for this asset; printing metrics only.")

    os.makedirs(args.out_dir, exist_ok=True)
    daily = _daily_metrics(df)
    fname = f"live_execution_{args.asset.lower()}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    out_csv = os.path.join(args.out_dir, fname)
    daily.to_csv(out_csv, index=False)
    print(f"\nSaved daily summary to {out_csv}")


if __name__ == "__main__":
    main()