"""
A/B walk-forward: calibration with the pre-fix 100-row cap vs the fixed
proportional calibration (as-deployed), on IDENTICAL fold frames.

Motivation (2026-08-25): the old calibrate_model used
    min_calib = max(20, min(100, len(X_train)//6//2))
so every training set larger than ~1200 rows calibrated on exactly 100 rows
(0.2% of the real XAUUSD set). The sigmoid fitted there collapsed the
probability spread and killed the short side. This script reproduces that old
regime through the optional `calibration_calib_rows_cap` hook added to
model/trainer.calibrate_model and measures long vs short PnL on the SAME folds.

Fold frames come from the shared honest splitter (`scripts.deflated_sharpe._build_fold_frames`),
so purge / embargo / per-fold fresh model training match the standard harness
bit-for-bit. Only the calibration cap differs between the two legs.

Run:
    python scripts/diag_calib_ab.py --asset XAUUSD --cap 100
Output:
    logs/diag_calib_ab_<asset>_cap<cap>.csv   (per-trade records for capped leg)
  plus console comparison of the two legs' long/short metrics.
"""

import argparse
import copy
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.deflated_sharpe import (
    _apply_variant,
    _build_fold_frames,
    _variants_for,
    _prepare_fold_frame,
    _score_fold,
)
from scripts.diag_direction_split import _metrics_for
from scripts.run_backtest import load_asset_history, build_full_df, merge_asset_cfg, truncate_before
from scripts.train_mt5 import build_full_df as prod_build_full_df
from model.ensemble_backtest import EnsembleBacktester


def collect_leg(cfg, asset_key, df_full, variant_name, overrides, cap, max_folds=None):
    """One leg: score every fold with the calibration cap (or fix if cap is None).
    Records are tagged by entry direction (the EnsembleBacktester decides bias)."""
    legged_cfg = _apply_variant(cfg, asset_key, overrides)
    if cap is not None:
        # inject the cap into the per-fold model config the way run_backtest does
        cfg_with_cap = copy.deepcopy(legged_cfg)
        cfg_with_cap.setdefault("assets", {}).setdefault(asset_key, {}).setdefault("model", {})[
            "calibration_calib_rows_cap"
        ] = int(cap)
        legged_cfg = cfg_with_cap

    windows, frames = _build_fold_frames(df_full, legged_cfg, asset_key, max_folds)
    bt_cfg = legged_cfg.get("backtest", {})
    pv = legged_cfg.get("assets", {}).get(asset_key, {}).get(
        "point_value_lot", bt_cfg.get("point_value_lot", 100.0)
    )

    records = []
    for fold_i, fdf in enumerate(frames):
        fdf_run = _prepare_fold_frame(fdf, variant_name, fold_i, 42)
        cfg_run = merge_asset_cfg(legged_cfg, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "model")

        engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
        trades = engine.run(fdf_run.reset_index(drop=True))
        ts_series = fdf_run["timestamp_utc"].reset_index(drop=True)
        for t in trades:
            matches = ts_series[ts_series == t.entry_ts]
            if matches.empty:
                continue
            row = fdf_run.iloc[matches.index[0]]
            pl = float(row.get("ml_p_long", 0.5))
            ps = float(row.get("ml_p_short", 0.5))
            risk_money = abs(t.entry_price - t.initial_stop_price) * t.volume * pv
            r = float(t.pnl / risk_money) if risk_money > 1e-12 else 0.0
            records.append({
                "fold_id": fold_i,
                "direction": "long" if str(t.direction).lower().startswith("long") else "short",
                "p_max": max(pl, ps),
                "R": round(r, 6),
                "pnl": round(float(t.pnl), 6),
            })
    return records


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", default="XAUUSD")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--variant", default="current")
    parser.add_argument("--cap", type=int, help="Calibration cap (set for the 'pre-fix' leg; omit for the fixed leg)")
    parser.add_argument("--end-date", default="2026-08-08")
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out-dir", default="logs")
    args = parser.parse_args(argv)

    cfg = load_config()
    asset_key = args.asset
    family = _variants_for(asset_key)
    if args.variant not in family:
        raise SystemExit(f"Unknown variant; available: {list(family)}")

    timeframe = cfg["assets"][asset_key].get("timeframe", "M15")
    db = args.db_path or cfg.get("general", {}).get("db_path")
    raw = load_asset_history(db, timeframe, asset_key)
    if args.end_date:
        raw = truncate_before(raw, args.end_date, asset_key)
    df_full = prod_build_full_df(raw, cfg, db_path=db, asset_key=asset_key, timeframe=timeframe)
    print(f"Loaded {len(df_full)} rows for {asset_key} ({timeframe}), end {args.end_date}")

    records = collect_leg(cfg, asset_key, df_full, args.variant, family[args.variant],
                          args.cap, max_folds=args.max_folds)

    out_path = os.path.join(args.out_dir, f"diag_calib_ab_{asset_key.lower()}_cap{args.cap}.csv")
    pd.DataFrame(records).to_csv(out_path, index=False)
    print(f"Saved per-trade leg data to {out_path}")

    m = _metrics_for(pd.DataFrame(records))
    print(f"\n=== leg cap={args.cap} ===")
    print(m.to_dict())


if __name__ == "__main__":
    main()