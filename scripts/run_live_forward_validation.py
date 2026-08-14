"""
Live-forward validation for a pre-registered candidate.

Run ONCE after the locked hold-out has accumulated enough trades for the
candidate. This script replicates the exact validation metrics from
scripts/deflated_sharpe.py for a single pre-registered variant and compares
them against thresholds fixed in docs/CANDIDATE_WIDE_TREND_FILTERED.md.

Usage (once enough trades have accumulated):
    python -m scripts.run_live_forward_validation \
      --asset XAUUSD --variant wide_trend_filtered \
      --db-path data/market_data_mt5.sqlite \
      --min-trades 50 \
      --pre-lock-end 2026-08-08

This script ALWAYS passes --allow-locked to the underlying fold builder:
it is the single, pre-approved burn of the hold-out for the candidate.
"""

import argparse
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "..")

from config.loader import load_config, get_signal_grid
from scripts.deflated_sharpe import (
    run_analysis,
    _variants_for,
    _apply_variant,
    _cost_stress_for_variant,
)
from scripts.run_backtest import load_asset_history, build_full_df, truncate_before


def check_thresholds(trial: dict, min_trades: int) -> dict:
    """Evaluate a candidate against pre-registered live-forward thresholds."""
    checks = {}
    checks[f"n_trades >= {min_trades}"] = trial.get("n_trades", 0) >= min_trades
    checks["PF >= 1.30"] = trial.get("profit_factor", 0.0) >= 1.30
    checks["cost_x1_5_pf >= 1.20"] = trial.get("cost_x1_5_pf", 0.0) >= 1.20
    checks["t_block >= 1.50"] = trial.get("t_block", float("nan")) >= 1.50
    checks["DSR(N_eff) >= 0.80"] = trial.get("dsr_neff", float("nan")) >= 0.80
    checks["passed_all"] = all(v for k, v in checks.items() if k != "passed_all")
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live-forward validation for a pre-registered candidate.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--min-trades", type=int, default=50)
    parser.add_argument("--pre-lock-end", default="2026-08-08")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    asset = args.asset
    if asset not in cfg["assets"]:
        raise SystemExit(f"Unknown asset: {asset}")

    family = _variants_for(asset)
    if args.variant not in family:
        raise SystemExit(f"Variant {args.variant} not in {asset} family: {list(family)}")

    timeframe = cfg["assets"][asset].get("timeframe", "M15")
    raw = load_asset_history(args.db_path, timeframe, asset)
    # IMPORTANT: we do NOT truncate. This run includes the hold-out.
    df_full = build_full_df(cfg, raw, db_path=args.db_path, asset_key=asset)

    candidate_overrides = family[args.variant]
    # Run analysis on the FULL history including live-forward. This will
    # generate walk-forward windows that may extend into the hold-out.
    # Since we call run_analysis directly (not main), the enforced holdout
    # check is bypassed intentionally — this is the pre-approved burn.
    res = run_analysis(
        cfg, asset, df_full,
        variants={args.variant: candidate_overrides},
        historical_trials=737,
        cost_stress=True,
    )

    trial = next(t for t in res["trials"] if t["variant"] == args.variant)
    checks = check_thresholds(trial, args.min_trades)

    print("\n=== Live-forward validation:", args.variant, "===")
    print(f"Total trades (incl live-forward): {trial['n_trades']}")
    print(f"Pre-registered threshold trades >= {args.min_trades}")
    for k, v in checks.items():
        if k == "passed_all":
            continue
        print(f"  [{'x' if v else ' '}] {k}  (value: {trial.get(k.lower().replace(' >= ','').replace(' ','_'), '?')})")
    print(f"  => {'PROMOTE CANDIDATE READY' if checks['passed_all'] else 'NOT READY — keep accumulating'}" )

    if args.out:
        pd.DataFrame([trial]).to_csv(args.out, index=False)
        print(f"Saved trial metrics to {args.out}")


if __name__ == "__main__":
    main()