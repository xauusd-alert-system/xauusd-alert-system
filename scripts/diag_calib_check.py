"""
Calibration health check for a deployed direction model.

Compares RAW XGBoost probabilities (base estimator) against the CALIBRATED
probabilities (sigmoid/isotonic) on recent real bars, to verify a model is
not suffering from the 2026-08-12 calibration defect:

  * sigmoid fit on ~100 rows (0.2% of fit set) collapsed the probability
    spread and INVERTED the signal (negative a_), making shorts
    mathematically impossible on some assets (XAUUSD: 0/400 bars).

A healthy model must:
  * produce calibrated p_short > 0.5 on a meaningful share of bars
    (shorts must be reachable, not structurally suppressed);
  * agree with the raw model's argmax direction on almost every bar
    (no inversion — a flip rate near 50% means calibration inverts);
  * not pin probabilities to 0/1 (moderate shrinkage is expected).

Usage:
    python scripts/diag_calib_check.py --asset BTCUSD
    python scripts/diag_calib_check.py --asset EURUSD

For M5 assets the history is capped to --cap-days (default 90) so the feature
build stays cheap; rolling features at the tail (the part we predict) are
complete regardless of the cap.
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.run_backtest import load_asset_history
from scripts.train_mt5 import build_full_df  # production builder: includes bifurcation features
from model.predictor import ModelPredictor


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Raw vs calibrated probability health check.")
    parser.add_argument("--asset", required=True, help="Asset key (e.g. BTCUSD)")
    parser.add_argument("--db-path", default=None, help="SQLite DB path")
    parser.add_argument("--cap-days", type=int, default=90,
                        help="Only keep the last N days of raw history (cheap feature build).")
    parser.add_argument("--bars", type=int, default=400,
                        help="How many tail bars to score.")
    args = parser.parse_args(argv)

    cfg = load_config()
    asset_key = args.asset
    if asset_key not in cfg.get("assets", {}):
        raise SystemExit(f"Unknown asset: {asset_key}")

    asset_cfg = cfg["assets"][asset_key]
    timeframe = asset_cfg.get("timeframe") or cfg.get("market_data", {}).get("timeframe", "M5")
    model_path = asset_cfg["model_path"]
    db_path = args.db_path or cfg.get("general", {}).get("db_path")

    raw = load_asset_history(db_path, timeframe, asset_key)
    if args.cap_days:
        cutoff = raw["timestamp_utc"].max() - args.cap_days * 86400.0
        raw = raw[raw["timestamp_utc"] >= cutoff]
    df = build_full_df(raw, cfg, db_path=db_path, asset_key=asset_key, timeframe=timeframe)
    print(f"Loaded {len(df)} rows for {asset_key} ({timeframe}); scoring tail {args.bars} bars")

    predictor = ModelPredictor(model_path)
    base = predictor.model.estimator  # raw XGBoost inside the calibrated wrapper

    tail = df.tail(args.bars).copy()
    feat_cols = predictor.feature_cols
    complete = tail[feat_cols].notna().all(axis=1)
    tail = tail[complete]
    if len(tail) == 0:
        print("No complete-feature rows in tail.")
        return

    X = tail[feat_cols].astype(float)
    raw_proba = base.predict_proba(X)
    # classes_ ordering: label 1 = long-favorable (see predictor docstring).
    classes = predictor.classes_
    if classes is None:
        classes = predictor.model.classes_
    # map to p_long / p_short by class label
    col_long = np.where(classes == 1)[0][0]
    col_short = np.where(classes == 0)[0][0]
    raw_p1 = raw_proba[:, col_long]
    cal = predictor.predict_proba(tail)
    cal_p1 = cal["p_long"].to_numpy()

    raw_short = (raw_p1 < 0.5).mean()
    cal_short = (cal_p1 < 0.5).mean()
    # flip = raw argmax disagrees with calibrated argmax
    flip = ((raw_p1 < 0.5) != (cal_p1 < 0.5)).mean()
    agree = 1.0 - flip
    shrink = float(np.mean(np.abs(cal_p1 - raw_p1)))

    print(f"\n=== {asset_key} calibration health ===")
    print(f"  bars scored: {len(tail)} (last {df['timestamp_utc'].max() - df['timestamp_utc'].min():.0f}d span)")
    print(f"  raw model:      p_short>0.5 on {100 * raw_short:.1f}% of bars")
    print(f"  calibrated:     p_short>0.5 on {100 * cal_short:.1f}% of bars")
    print(f"  direction agree: {100 * agree:.1f}%  (flip rate {100 * flip:.1f}%)")
    print(f"  mean |p_cal - p_raw|: {shrink:.4f}  (0=no shrinkage, ~0.5=heavy collapse)")
    print(f"  calibrated p_short distribution:")
    for thr in (0.50, 0.55, 0.60, 0.66, 0.70):
        share = (cal_p1 < 1 - thr).mean() if thr < 1 else 0.0
        print(f"    p_short > {thr:.2f}: {100 * share:.1f}% of bars")
    print(f"  calibrated p_short extremes: min={1 - cal_p1.max():.3f} max={1 - cal_p1.min():.3f}")


if __name__ == "__main__":
    main()
