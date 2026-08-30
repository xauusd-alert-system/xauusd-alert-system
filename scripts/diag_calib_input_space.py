"""
A/B: Platt sigmoid calibrated on RAW PROBABILITY vs LOGIT MARGIN (XAUUSD M15).

Audit 2026-08-25: XGBClassifier exposes no ``decision_function``, so sklearn's
``_SigmoidCalibration`` was fed ``predict_proba[:,1]`` (raw probability, a value
already squashed near 0.5). The sigmoid ``1/(1+exp(a*x+b))`` fit on raw p
collapsed the calibrated spread (std ~0.01, 95% of forecasts in 0.5-0.6) and
killed the minority direction. Canonical Platt scaling fits on the log-odds
margin ``logit(p) = ln(p/(1-p))``.

This script fits BOTH variants with the SAME production ``calibrate_model``
(callers pass raw XGB vs LogitMarginEstimator-wrapped XGB) and compares on the
SAME data: in-sample (fit), calibration-holdout, and a strictly later OOS tail.

Usage:
    python -m scripts.diag_calib_input_space [--bars 48738]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from config.loader import load_config
from model.calibration import compute_ece
from model.trainer import (
    build_training_matrix,
    calibrate_model,
    train_model,
)
from scripts.train_mt5 import build_full_df


def _eval(proba_p1, y01, label):
    ece, _ = compute_ece(y01, proba_p1, n_bins=10)
    std = float(np.std(proba_p1))
    q = np.quantile(proba_p1, [0.05, 0.5, 0.95])
    cov = {th: float((proba_p1 >= th).mean()) for th in (0.55, 0.60, 0.62, 0.70)}
    print(f"  [{label}] std_p={std:.4f} p5/p50/p95={q[0]:.3f}/{q[1]:.3f}/{q[2]:.3f}")
    print(
        f"            ECE={ece:.4f}  cov>=0.55:{cov[0.55]:.1%} "
        f">=0.60:{cov[0.60]:.1%} >=0.62:{cov[0.62]:.1%} >=0.70:{cov[0.70]:.1%}"
    )
    return {"std_p": std, "ece": ece, "coverage": cov}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=48738)
    parser.add_argument("--asset", default="XAUUSD")
    parser.add_argument(
        "--oos-bars", type=int, default=4000, help="Strictly-later out-of-sample tail (excluded from training)"
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    db = cfg["general"]["db_path"]
    asset = args.asset
    tf = cfg["assets"][asset].get("timeframe") or cfg["market_data"]["timeframe"]

    print(f"Loading {asset} {tf} ...")
    from scripts.run_backtest import load_asset_history

    raw = load_asset_history(db, tf, asset)
    raw = raw.tail(args.bars).reset_index(drop=True)
    df = build_full_df(raw, cfg, db_path=db, asset_key=asset, timeframe=tf)
    X, y, cols = build_training_matrix(df, cfg=cfg)
    print(f"Training matrix: {len(X)} rows x {len(cols)} cols, classes={sorted(y.unique())}")

    # binary long-favorable indicator (label 2 == long in 3-class, 1 in binary)
    y01 = (y == 2).astype(int) if y.nunique() == 3 else y.astype(int)

    # Strictly-later OOS tail; train on everything before it.
    if args.oos_bars and args.oos_bars < len(X):
        oos_idx = np.arange(len(X) - args.oos_bars, len(X))
        train_idx = np.arange(len(X) - args.oos_bars)
    else:
        oos_idx = np.array([], dtype=int)
        train_idx = np.arange(len(X))
    print(f"train rows={len(train_idx)} oos rows={len(oos_idx)}")

    def run_variant(variant: str, cal_input_space: str):
        cfg_v = dict(cfg)
        cfg_v["model"] = dict(cfg["model"])
        cfg_v["model"]["calibration_input_space"] = cal_input_space
        base = train_model(X.iloc[train_idx], y01.iloc[train_idx], cfg_v)
        cal = calibrate_model(base, X.iloc[train_idx], y01.iloc[train_idx], cfg_v)
        # predict_proba returns an (n, 2) ndarray, columns ordered by classes_.
        # Map by VALUE: column holding class 1 = long-favorable.
        classes = np.asarray(cal.classes_).astype(int)
        long_col = int(np.where(classes == 1)[0][0])
        p_tr = cal.predict_proba(X.iloc[train_idx])[:, long_col]
        p_oos = cal.predict_proba(X.iloc[oos_idx])[:, long_col] if len(oos_idx) else None
        cc = cal.calibrated_classifiers_[0]
        cb = getattr(cc, "calibrators", None) or getattr(cc, "calibrator", None)
        cb0 = cb[0] if isinstance(cb, list) else cb
        a_ = getattr(cb0, "a_", None)
        b_ = getattr(cb0, "b_", None)
        print(
            f"\n=== {variant} ===  a_={a_ if a_ is None else round(float(a_), 4)} "
            f"b_={b_ if b_ is None else round(float(b_), 4)}"
        )
        _eval(p_tr, y01.iloc[train_idx], "TRAIN  ")
        if p_oos is not None:
            _eval(p_oos, y01.iloc[oos_idx], "OOS    ")
        return p_tr, p_oos

    print("\n############ RAW-PROBABILITY CALIBRATION (pre-fix) ############")
    old_tr, old_oos = run_variant("RAW PROB (pre-fix)", "raw_probability")

    print("\n############ LOGIT-MARGIN CALIBRATION (post-fix) ############")
    new_tr, new_oos = run_variant("LOGIT MARGIN (post-fix)", "logit_margin")

    if old_oos is not None and new_oos is not None:
        print("\n############ DELTA on OOS (post-fix vs pre-fix) ############")
        print(
            f"  OOS std_p:  {np.std(old_oos):.4f} -> {np.std(new_oos):.4f} "
            f"(x{np.std(new_oos) / max(np.std(old_oos), 1e-9):.2f})"
        )
        ece_old, _ = compute_ece(y01.iloc[oos_idx], old_oos, n_bins=10)
        ece_new, _ = compute_ece(y01.iloc[oos_idx], new_oos, n_bins=10)
        print(f"  OOS ECE:    {ece_old:.4f} -> {ece_new:.4f}")
        for th in (0.55, 0.60, 0.62, 0.70):
            cov_o = float((old_oos >= th).mean())
            cov_n = float((new_oos >= th).mean())
            print(f"  OOS cov>={th}: {cov_o:.1%} -> {cov_n:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
