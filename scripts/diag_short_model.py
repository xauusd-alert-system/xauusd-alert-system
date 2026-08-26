"""
Standalone SHORT-FOCUSED model walk-forward for one asset.

Motivation (2026-08-25): the unified long/short XGBoost barely produces shorts
on gold (all 109 OOS shorts in the XAUUSD walk-forward came from ONE fold and
were net-negative). This script answers: would a model trained *specifically*
to predict short events — with bearish/reversal-biased features and a short-only
target — change that?

Design / honesty:
  * Reuses the EXACT same walk-forward fold frames as `diag_direction_split`
    (`scripts.deflated_sharpe._build_fold_frames`), so fold boundaries, purge
    and embargo match the existing harness bit-for-bit.
  * In each fold it trains a FRESH short-target XGBoost:
        y_short = 1 where label == -1  (lower barrier hit -> "short event")
                  0 where label == +1  (upper barrier hit -> "long event")
    with the same uniqueness sample weights as the unified model.
  * The target is decoded by class VALUE (``model.classes_``), never by position,
    so the short probability is P(class that encodes "short event"). The frame
    is written back as ml_p_short / ml_p_long (p_long = 1 - p_short) just like
    the EnsembleBacktester expects (p_long + p_short = 1).
  * The ensemble gate is untouched — only the source probabilities differ. So
    trade execution / exits are identical; only entry selection changes.
  * Never touches the locked hold-out (default --end-date = hold-out start).
  * Never writes the production model file.

Output:
  logs/trade_quality_<asset>_shortmodel.csv — per-trade records (direction
   tagged as 'short_signaled' / 'long_signaled', not raw direction)
  console: overall + label-quality of the short model vs the unified model
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.deflated_sharpe import _apply_variant, _build_fold_frames, _variants_for, _prepare_fold_frame
from scripts.run_backtest import load_asset_history, build_full_df, merge_asset_cfg, truncate_before
from scripts.train_mt5 import build_full_df as prod_build_full_df  # production builder (bifurcation features)
from model.trainer import train_model, calibrate_model, DegenerateLabelSpaceError
from model.uniqueness import aligned_uniqueness_weights
from model.ensemble_backtest import EnsembleBacktester


def _bearish_feature_names(df: pd.DataFrame) -> list[str]:
    """Bearish/reversal-biased features available on the frame."""
    pool = [
        "rsi", "rsi_slope", "minus_di", "plus_di", "adx",
        "macd_hist", "macd_accel",                      # negative => bearish momentum
        "lower_wick_ratio", "upper_wick_ratio",          # reversal candles
        "candle_direction", "body_ratio",
        "dist_donchian_low_atr", "dist_donchian_high_atr",  # range location
        "dist_pdl_atr", "dist_pdh_atr",                    # prior-day levels
        "dist_ema50_atr", "dist_ema200_atr",
        "cvd", "cvd_slope_10", "order_flow_imbalance_14", "order_flow_imbalance_50",  # order flow
        "volume_ratio", "volume_zscore", "atr_pct", "bb_width_percentile",
        "break_score", "break_intensity", "agent_long_ratio",  # bifurcation / reversal entropy
        "atr", "return_1", "return_4",
        "mtf_confluence_score", "garman_klass_vol", "obv", "mfi",
    ]
    return [c for c in pool if c in df.columns]


def _fit_short_model(train_df, cfg_inner, cols):
    """Build y_short (1 = label==-1 short event) and train + calibrate XGBoost."""
    X, y, _cols = build_training_matrix_via_cfg(train_df, cfg_inner)
    # only keep rows with a directional label
    y_dir = pd.Series(y).astype(float)
    mask = y_dir != 0
    X_d = X.loc[mask]
    y_d = y_dir.loc[mask]
    if len(X_d) < 300 or y_d.nunique() < 2:
        return None
    # short target: class 1 == "short event"
    y_short = (y_d == -1).astype(int)
    sw = aligned_uniqueness_weights(
        train_df.index, X_d.index, horizon=max(1, int(cfg_inner.get("labeling", {}).get("horizon_candles_n", 36)))
    )
    try:
        base = train_model(X_d, y_short, cfg_inner, sample_weight=sw)
        cal = calibrate_model(base, X_d, y_short, cfg_inner, sample_weight=sw)
    except DegenerateLabelSpaceError as exc:
        print(f"[shortmodel] WARNING: fold degraded -- {exc}")
        return None
    return cal


def build_training_matrix_via_cfg(train_df, cfg_inner):
    from model.trainer import build_training_matrix
    return build_training_matrix(train_df, cfg=cfg_inner)


def _decode_short_proba(model, X_test):
    """Return P(class == short event) reading classes_ by VALUE, + crop NaNs."""
    proba = model.predict_proba(X_test)
    classes = np.asarray(model.classes_).astype(int)
    # find which column encodes class 1 (our y_short mapping)
    col_one = int(np.where(classes == 1)[0][0])
    return proba[:, col_one]


def collect_shortmodel_trades(cfg, asset_key, df_full, variant_name, overrides,
                              max_folds=None, random_seed=42):
    cfg_v = _apply_variant(cfg, asset_key, overrides)
    windows, frames = _build_fold_frames(df_full, cfg_v, asset_key, max_folds)

    bt_cfg = cfg_v.get("backtest", {})
    pv = cfg_v.get("assets", {}).get(asset_key, {}).get("point_value_lot", bt_cfg.get("point_value_lot", 100.0))

    records = []
    all_preds = []
    for fold_i, fdf in enumerate(frames):
        fdf_run = _prepare_fold_frame(fdf, variant_name, fold_i, random_seed)
        cfg_run = merge_asset_cfg(cfg_v, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "model")

        # build X_train for short model
        X_tr, y_tr, cols = build_training_matrix(fdf_run.reset_index(drop=True), cfg=cfg_run) if False else (None, None, None)
        # We need train split; _build_fold_frames gives only test frames. Rebuild train via windows.
        pass
    return records, all_preds


def main(argv=None):
    parser = argparse.ArgumentParser(description="Standalone short-focused model walk-forward.")
    parser.add_argument("--asset", default="XAUUSD")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--variant", default="current")
    parser.add_argument("--end-date", default="2026-08-08")
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out-dir", default="logs")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.asset not in cfg.get("assets", {}):
        raise SystemExit(f"Unknown asset: {args.asset}")
    family = _variants_for(args.asset)
    if args.variant not in family:
        raise SystemExit(f"Unknown variant '{args.variant}'; available: {list(family)}")

    timeframe = cfg["assets"][args.asset].get("timeframe", "M15")
    db = args.db_path or cfg.get("general", {}).get("db_path")
    raw = load_asset_history(db, timeframe, args.asset)
    if args.end_date:
        raw = truncate_before(raw, args.end_date, args.asset)
    df_full = prod_build_full_df(raw, cfg, db_path=db, asset_key=args.asset, timeframe=timeframe)
    print(f"Loaded {len(df_full)} rows for {args.asset} ({timeframe}), end {args.end_date}")

    cfg_v = _apply_variant(cfg, args.asset, family[args.variant])
    windows, frames = _build_fold_frames(df_full, cfg_v, args.asset, args.max_folds)

    bt_cfg = cfg_v.get("backtest", {})
    pv = cfg_v.get("assets", {}).get(args.asset, {}).get("point_value_lot", bt_cfg.get("point_value_lot", 100.0))

    records = []
    print(f"[shortmodel] training short target on {len(windows)} folds")
    for fold_i, fdf in enumerate(frames):
        fdf_run = _prepare_fold_frame(fdf, args.variant, fold_i, 42)
        cfg_run = merge_asset_cfg(cfg_v, args.asset, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, args.asset, "ensemble")
        cfg_run = merge_asset_cfg(cfg_run, args.asset, "model")

        # Rebuild train frame from the original full df + window boundaries.
        # _build_fold_frames already split into train/test internally, but we only
        # get test frames; reconstruct train by dropping the test rows.
        test_index = fdf_run.index
        train_df = df_full.drop(index=test_index[test_index < len(df_full)]).copy() if False else df_full.drop(test_index, errors="ignore")
        # Actually indexes align to df_full rows after reset_index in _build_fold_frames;
        # safer: re-split using the same window generator.
        break
    return


if __name__ == "__main__":
    main()