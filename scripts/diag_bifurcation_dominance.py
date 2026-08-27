"""Does the bifurcation feature set dominate the XAUUSD model?

Answers two questions on the SAME OOS window (production data path):

  1. FEATURE IMPORTANCE (deployed model): where do break_score /
     break_intensity / agent_long_ratio rank among the 49 features?
  2. AUC A/B: refit the production XGBoost (identical hyper-parameters and
     training slice) with the full 49-feature set vs the 46-feature set
     WITHOUT the bifurcation trio, and compare OOS AUC / PR-AUC.

The split mirrors production semantics: time-ordered fit slice -> purge gap
(labeling horizon) -> OOS tail.

Usage:
    python -m scripts.diag_bifurcation_dominance
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import auc as sk_auc
from sklearn.metrics import precision_recall_curve, roc_auc_score

from config.loader import load_config, resolve_asset_timeframe
from model.trainer import _get_xgb_classifier
from scripts.retrain_with_real_trades import read_candles
from scripts.train_mt5 import build_full_df, build_training_matrix

BIFURC_FEATURES = ["break_score", "break_intensity", "agent_long_ratio"]


def _split(df: pd.DataFrame, gap: int, oos_bars: int):
    """Time-ordered: fit | purge | OOS (positional, last oos_bars rows)."""
    end = len(df)
    oos_start = max(0, end - oos_bars)
    cal_stop = max(0, oos_start - gap)
    fit_stop = max(0, cal_stop - gap)
    return fit_stop, cal_stop, oos_start, end


def main() -> int:
    cfg = load_config()
    asset = "XAUUSD"
    acfg = cfg["assets"][asset]
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    timeframe = resolve_asset_timeframe(cfg, asset)
    model_path = Path(acfg["model_path"])
    if not model_path.exists():
        print(f"FAIL: model not found: {model_path}")
        return 1

    bundle = joblib.load(model_path)
    model = bundle["model"]
    cols = bundle.get("feature_cols", [])
    print(f"=== bifurcation dominance: {asset} ({timeframe}) ===")

    # ---- 1. importance of the DEPLOYED model ----
    cc = model.calibrated_classifiers_[0]
    base = cc.estimator
    try:
        fi = np.asarray(base.feature_importances_, dtype=float)
    except AttributeError:
        print("deployed base has no feature_importances_")
        fi = None
    if fi is not None and len(fi) == len(cols):
        order = np.argsort(fi)[::-1]
        print("\n[1] deployed XGB feature importance (gain)")
        print(f"    {'feature':<30}{'imp':>8}  rank")
        for name in BIFURC_FEATURES:
            idx = cols.index(name)
            rank = int(np.where(order == idx)[0][0]) + 1
            print(f"    {name:<30}{fi[idx]:>8.4f}  {rank}/{len(cols)}")
        top = order[:10]
        print("    top-10: " + ", ".join(f"{cols[i]}({fi[i]:.3f})" for i in top))

    # ---- 2. AUC A/B on the same OOS window ----
    print("\nbuilding features (production path) ...", flush=True)
    raw = read_candles(db_path, timeframe, asset)
    if raw is None or raw.empty:
        print("FAIL: no candles")
        return 1
    full = build_full_df(raw, cfg, db_path, asset, timeframe)
    X_all, y_all, avail = build_training_matrix(full, cfg=cfg)
    print(f"rows={len(X_all)}  features={len(avail)}")

    missing = [f for f in BIFURC_FEATURES if f not in avail]
    if missing:
        print(f"FAIL: bifurcation features missing from training matrix: {missing}")
        return 1

    model_cfg = cfg["model"]
    random_state = model_cfg.get("random_seed", 42)
    horizon = int(model_cfg.get("labeling", {}).get("horizon_candles_n", 0)) or \
        int(cfg.get("labeling", {}).get("horizon_candles_n", 6))
    gap = max(horizon, int(model_cfg.get("purge_gap_bars", 36)))
    oos_bars = 1000

    fit_stop, cal_stop, oos_start, end = _split(X_all, gap, oos_bars)
    print(f"split: fit=[0:{fit_stop}] cal=[{fit_stop}:{cal_stop}] "
          f"oos=[{oos_start}:{end}]  purge_gap={gap}")

    # full-feature model
    Xf, yf = X_all.iloc[:fit_stop], y_all.iloc[:fit_stop]
    Xo, yo = X_all.iloc[oos_start:end], y_all.iloc[oos_start:end]

    results = {}
    for label, keep in (("full (49)", True), ("no-bifurc (46)", False)):
        Xt = Xf if keep else Xf.drop(columns=[c for c in BIFURC_FEATURES if c in Xf.columns])
        Xt_oos = Xo if keep else Xo.drop(columns=[c for c in BIFURC_FEATURES if c in Xo.columns])
        m = _get_xgb_classifier(model_cfg, random_state)
        m.fit(Xt, yf)
        p = m.predict_proba(Xt_oos)[:, 1]
        auc = float(roc_auc_score(yo, p))
        prec, rec, _ = precision_recall_curve(yo, p)
        pr_auc = float(sk_auc(rec, prec))
        # calibration proxy on OOS (raw, no Platt): ECE via deciles
        bins = np.linspace(0, 1, 11)
        ece = 0.0
        for lo, hi in zip(bins[:-1], bins[1:]):
            msk = (p > lo) & (p <= hi) if lo > 0 else (p >= lo) & (p <= hi)
            if msk.any():
                ece += (msk.sum() / len(p)) * abs(yo[msk].mean() - p[msk].mean())
        results[label] = {"auc": auc, "pr_auc": pr_auc, "ece": ece, "n": len(yo)}
        print(f"\n[2] {label}: OOS n={len(yo)}  AUC={auc:.4f}  PR-AUC={pr_auc:.4f}  "
              f"ECE={ece:.4f}")

    f_, n_ = results["full (49)"], results["no-bifurc (46)"]
    print("\n[3] delta (full - no-bifurc):")
    print(f"    AUC    {f_['auc'] - n_['auc']:+.4f}  "
          f"({'bifurcation HELPS' if f_['auc'] > n_['auc'] else 'bifurcation HURTS'})")
    print(f"    PR-AUC {f_['pr_auc'] - n_['pr_auc']:+.4f}")
    print(f"    ECE    {f_['ece'] - n_['ece']:+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
