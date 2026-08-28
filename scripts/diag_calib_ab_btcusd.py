#!/usr/bin/env python3
"""
A/B calibration diagnostic for BTCUSD.

Compares two calibration regimes on IDENTICAL walk-forward folds:
  A) cap100: calibration_calib_rows_cap=100 (old regime)
  B) proportional: 15% holdout, no cap (current regime)

Measures: sigmoid params, p_short spread, edge distribution, direction balance.

Run:
    python -m scripts.diag_calib_ab_btcusd
"""

import copy
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.deflated_sharpe import _build_fold_frames
from scripts.run_backtest import build_full_df, load_asset_history


def _sigmoid_params(model):
    """Extract Platt sigmoid a, b from a CalibratedClassifierCV model."""
    cc = model.calibrated_classifiers_[0]
    cal = cc.calibrators_[0]
    return getattr(cal, "a_", 0.0), getattr(cal, "b_", 0.0)


def _get_calibration_info(model):
    """Get calibration slice info from the model."""
    cc = model.calibrated_classifiers_[0]
    cal = cc.calibrators_[0]
    a, b = getattr(cal, "a_", 0.0), getattr(cal, "b_", 0.0)
    n_bins = getattr(cal, "n_bins", 0)
    return {"a": a, "b": b, "n_bins": n_bins}


def _make_model_with_cap(cfg, cap):
    """Create a config with calibration_calib_rows_cap."""
    cfg_cap = copy.deepcopy(cfg)
    if cap is not None:
        cfg_cap.setdefault("model", {})["calibration_calib_rows_cap"] = int(cap)
    else:
        cfg_cap.setdefault("model", {}).pop("calibration_calib_rows_cap", None)
    return cfg_cap


def main():
    cfg = load_config()
    asset_key = "BTCUSD"
    tf = cfg.get("assets", {}).get(asset_key, {}).get("timeframe", "M5")
    db = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    print("=" * 60)
    print(f"A/B CALIBRATION DIAGNOSTIC: {asset_key} ({tf})")
    print("=" * 60)

    # Load data
    raw = load_asset_history(db, tf, asset_key)
    df_full = build_full_df(cfg, raw, db_path=db, asset_key=asset_key)
    print(f"Data: {len(df_full)} bars")

    # Build folds with cap100
    cfg_cap100 = _make_model_with_cap(cfg, 100)
    windows_cap, frames_cap = _build_fold_frames(df_full, cfg_cap100, asset_key, 5)

    # Build folds with proportional (no cap)
    cfg_prop = _make_model_with_cap(cfg, None)
    windows_prop, frames_prop = _build_fold_frames(df_full, cfg_prop, asset_key, 5)

    print(f"Folds: cap100={len(frames_cap)}, proportional={len(frames_prop)}")
    print()

    # Compare sigmoid params per fold
    # The fold frames already have ml_p_long/ml_p_short injected
    # by _build_fold_frames. We need to extract the sigmoid from
    # the TRAINED model, but _build_fold_frames doesn't return it.
    # Instead, we measure the OUTPUT (p_long/p_short) directly.

    for fold_i in range(min(len(frames_cap), len(frames_prop))):
        fdf_cap = frames_cap[fold_i]
        fdf_prop = frames_prop[fold_i]

        pl_cap = fdf_cap.get("ml_p_long", pd.Series(0.5, index=fdf_cap.index)).values
        ps_cap = fdf_cap.get("ml_p_short", pd.Series(0.5, index=fdf_cap.index)).values
        pl_prop = fdf_prop.get("ml_p_long", pd.Series(0.5, index=fdf_prop.index)).values
        ps_prop = fdf_prop.get("ml_p_short", pd.Series(0.5, index=fdf_prop.index)).values

        edges_cap = np.abs(pl_cap - ps_cap)
        edges_prop = np.abs(pl_prop - ps_prop)
        pmax_cap = np.maximum(pl_cap, ps_cap)
        pmax_prop = np.maximum(pl_prop, ps_prop)

        long_dir_cap = (pl_cap > ps_cap).sum()
        short_dir_cap = (ps_cap > pl_cap).sum()
        long_dir_prop = (pl_prop > ps_prop).sum()
        short_dir_prop = (ps_prop > pl_prop).sum()

        min_edge = 0.15
        passes_cap = edges_cap >= min_edge
        passes_prop = edges_prop >= min_edge

        lp_cap = ((pl_cap > ps_cap) & passes_cap).sum()
        sp_cap = ((ps_cap > pl_cap) & passes_cap).sum()
        lp_prop = ((pl_prop > ps_prop) & passes_prop).sum()
        sp_prop = ((ps_prop > pl_prop) & passes_prop).sum()

        print(f"--- Fold {fold_i} ({len(fdf_cap)} bars) ---")
        print(f"  {'Metric':<32} {'cap100':>12} {'proportional':>12}")
        print(f"  {'-'*56}")
        print(f"  {'p_long mean':<32} {pl_cap.mean():>12.4f} {pl_prop.mean():>12.4f}")
        print(f"  {'p_long std':<32} {np.std(pl_cap):>12.4f} {np.std(pl_prop):>12.4f}")
        print(f"  {'p_short mean':<32} {ps_cap.mean():>12.4f} {ps_prop.mean():>12.4f}")
        print(f"  {'p_short std':<32} {np.std(ps_cap):>12.4f} {np.std(ps_prop):>12.4f}")
        print(f"  {'p_short range':<32} {ps_cap.max()-ps_cap.min():>12.4f} {ps_prop.max()-ps_prop.min():>12.4f}")
        print(f"  {'edge mean':<32} {edges_cap.mean():>12.4f} {edges_prop.mean():>12.4f}")
        print(f"  {'edge std':<32} {np.std(edges_cap):>12.4f} {np.std(edges_prop):>12.4f}")
        print(f"  {'edge >= 0.15':<32} {passes_cap.sum():>9}/{len(passes_cap)} {passes_prop.sum():>9}/{len(passes_prop)}")
        print(f"  {'dir: long / short':<32} {long_dir_cap:>5}/{short_dir_cap:<5} {long_dir_prop:>5}/{short_dir_prop:<5}")
        print(f"  {'after edge: long / short':<32} {lp_cap:>5}/{sp_cap:<5} {lp_prop:>5}/{sp_prop:<5}")
        print()

    # Summary across all folds
    print("=" * 60)
    print("AGGREGATE SUMMARY")
    print("=" * 60)

    all_pl_cap, all_ps_cap = [], []
    all_pl_prop, all_ps_prop = [], []

    for fdf_cap, fdf_prop in zip(frames_cap, frames_prop):
        all_pl_cap.extend(fdf_cap.get("ml_p_long", pd.Series(0.5)).values)
        all_ps_cap.extend(fdf_cap.get("ml_p_short", pd.Series(0.5)).values)
        all_pl_prop.extend(fdf_prop.get("ml_p_long", pd.Series(0.5)).values)
        all_ps_prop.extend(fdf_prop.get("ml_p_short", pd.Series(0.5)).values)

    all_pl_cap = np.array(all_pl_cap)
    all_ps_cap = np.array(all_ps_cap)
    all_pl_prop = np.array(all_pl_prop)
    all_ps_prop = np.array(all_ps_prop)

    edges_cap = np.abs(all_pl_cap - all_ps_cap)
    edges_prop = np.abs(all_pl_prop - all_ps_prop)

    long_cap = (all_pl_cap > all_ps_cap).sum()
    short_cap = (all_ps_cap > all_pl_cap).sum()
    long_prop = (all_pl_prop > all_ps_prop).sum()
    short_prop = (all_ps_prop > all_pl_prop).sum()

    passes_cap = edges_cap >= 0.15
    passes_prop = edges_prop >= 0.15
    lp_cap = ((all_pl_cap > all_ps_cap) & passes_cap).sum()
    sp_cap = ((all_ps_cap > all_pl_cap) & passes_cap).sum()
    lp_prop = ((all_pl_prop > all_ps_prop) & passes_prop).sum()
    sp_prop = ((all_ps_prop > all_pl_prop) & passes_prop).sum()

    print(f"\n  {'Metric':<32} {'cap100':>12} {'proportional':>12}")
    print(f"  {'-'*56}")
    print(f"  {'Total bars':<32} {len(all_pl_cap):>12} {len(all_pl_prop):>12}")
    print(f"  {'p_long mean':<32} {all_pl_cap.mean():>12.4f} {all_pl_prop.mean():>12.4f}")
    print(f"  {'p_long std':<32} {np.std(all_pl_cap):>12.4f} {np.std(all_pl_prop):>12.4f}")
    print(f"  {'p_short mean':<32} {all_ps_cap.mean():>12.4f} {all_ps_prop.mean():>12.4f}")
    print(f"  {'p_short std':<32} {np.std(all_ps_cap):>12.4f} {np.std(all_ps_prop):>12.4f}")
    print(f"  {'p_short range':<32} {all_ps_cap.max()-all_ps_cap.min():>12.4f} {all_ps_prop.max()-all_ps_prop.min():>12.4f}")
    print(f"  {'edge mean':<32} {edges_cap.mean():>12.4f} {edges_prop.mean():>12.4f}")
    print(f"  {'edge std':<32} {np.std(edges_cap):>12.4f} {np.std(edges_prop):>12.4f}")
    print(f"  {'edge >= 0.15':<32} {passes_cap.sum():>9}/{len(passes_cap)} {passes_prop.sum():>9}/{len(passes_prop)}")
    print(f"  {'dir: long / short':<32} {long_cap:>5}/{short_cap:<5} {long_prop:>5}/{short_prop:<5}")
    print(f"  {'after edge: long / short':<32} {lp_cap:>5}/{sp_cap:<5} {lp_prop:>5}/{sp_prop:<5}")

    # Spread expansion ratio
    std_cap = np.std(all_ps_cap)
    std_prop = np.std(all_ps_prop)
    print(f"\n  p_short spread expansion: {std_prop / max(std_cap, 1e-9):.2f}x")
    print(f"  edge spread expansion:   {np.std(edges_prop) / max(np.std(edges_cap), 1e-9):.2f}x")

    # Direction balance improvement
    ratio_cap = max(long_cap, short_cap) / max(min(long_cap, short_cap), 1)
    ratio_prop = max(long_prop, short_prop) / max(min(long_prop, short_prop), 1)
    print(f"  direction imbalance (max/min): cap100={ratio_cap:.1f}x  proportional={ratio_prop:.1f}x")

    # Save
    os.makedirs("logs", exist_ok=True)
    rdf = pd.DataFrame({
        "p_long_cap": all_pl_cap, "p_short_cap": all_ps_cap,
        "p_long_prop": all_pl_prop, "p_short_prop": all_ps_prop,
    })
    rdf.to_csv("logs/diag_calib_ab_btcusd.csv", index=False)
    print("\n  CSV -> logs/diag_calib_ab_btcusd.csv")


if __name__ == "__main__":
    main()
