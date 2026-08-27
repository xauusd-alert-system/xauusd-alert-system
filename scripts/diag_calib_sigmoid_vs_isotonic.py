"""A/B: Platt sigmoid (current production) vs isotonic-by-purged-CV-folds.

Both arms calibrate on the SAME training window and are scored on the SAME
untouched OOS tail (production data path). Metrics: ECE, Brier, sharpness
(std_p, mean |p-0.5|) and coverage at p>=0.55 / p>=0.60.

Isotonic arm uses time-ordered, PURGED expanding folds (no shuffled K-fold,
no temporal overlap) so the comparison is leakage-honest versus the
production single-split sigmoid.

Usage:
    python -m scripts.diag_calib_sigmoid_vs_isotonic
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV

from config.loader import load_config, resolve_asset_timeframe
from model.calibration import compute_brier_score, compute_ece
from model.trainer import LogitMarginEstimator, _get_xgb_classifier
from scripts.retrain_with_real_trades import read_candles
from scripts.train_mt5 import build_full_df, build_training_matrix


def purged_expanding_folds(n: int, n_folds: int, horizon: int, min_calib: int):
    """Expanding-window purged folds: fold k trains on earlier data, tests on
    the next chunk, with a `horizon`-row purge gap between train and test."""
    folds = []
    test_share = (n - min_calib) / n_folds
    for k in range(n_folds):
        test_end = int(n - (n_folds - 1 - k) * test_share)
        test_start = int(test_end - test_share)
        if test_start <= horizon + 1:
            continue
        train_idx = np.arange(0, test_start - horizon)
        test_idx = np.arange(test_start, test_end)
        if len(train_idx) < 50 or len(test_idx) < 20:
            continue
        folds.append((train_idx, test_idx))
    return folds


def sharpness(p: np.ndarray) -> dict:
    return {
        "std_p": float(np.std(p)),
        "mean_abs_dev_05": float(np.mean(np.abs(p - 0.5))),
    }


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
    cols = bundle.get("feature_cols", [])
    model_cfg = cfg["model"]
    random_state = model_cfg.get("random_seed", 42)

    print(f"=== sigmoid vs isotonic-by-CV-folds: {asset} ({timeframe}) ===")

    print("building features (production path) ...", flush=True)
    raw = read_candles(db_path, timeframe, asset)
    if raw is None or raw.empty:
        print("FAIL: no candles")
        return 1
    full = build_full_df(raw, cfg, db_path, asset, timeframe)
    X_all, y_all, _avail = build_training_matrix(full, cfg=cfg)
    print(f"rows={len(X_all)}  features={len(_avail)}")

    horizon = int(cfg.get("labeling", {}).get("horizon_candles_n", 24))
    n = len(X_all)
    oos_bars = 1000
    gap = max(horizon, int(model_cfg.get("purge_gap_bars", 36)))
    oos_start = max(0, n - oos_bars)
    train_stop = max(0, oos_start - gap)
    X_tr, y_tr = X_all.iloc[:train_stop], y_all.iloc[:train_stop]
    X_oo, y_oo = X_all.iloc[oos_start:], y_all.iloc[oos_start:]
    print(f"train rows={len(X_tr)}  oos rows={len(X_oo)}  purge={gap}")

    # ---- base XGB (same for both arms) ----
    base = _get_xgb_classifier(model_cfg, random_state)
    base.fit(X_tr, y_tr)
    print("base XGB fitted")

    results = {}

    # ---- ARM A: production sigmoid (logit margin, single purged split) ----
    split_indices = purged_expanding_folds(len(X_tr), 1, gap, max(20, len(X_tr) // 15))
    wrap = LogitMarginEstimator(base)
    cal_a = CalibratedClassifierCV(wrap, method="sigmoid", cv=split_indices)
    cal_a.fit(X_tr, y_tr)
    p_a = cal_a.predict_proba(X_oo)[:, 1]
    ece_a, _ = compute_ece(y_oo, p_a)
    results["sigmoid (prod)"] = {
        "ece": ece_a,
        "brier": compute_brier_score(y_oo, p_a),
        **sharpness(p_a),
        "cov55": float((p_a >= 0.55).mean()),
        "cov60": float((p_a >= 0.60).mean()),
        "a_": float(getattr(cal_a.calibrated_classifiers_[0].calibrators[0], "a_", float("nan"))),
    }

    # ---- ARM B: isotonic by purged CV folds (ensemble averaging) ----
    folds_b = purged_expanding_folds(len(X_tr), 3, gap, max(20, len(X_tr) // 15))
    wrap_b = LogitMarginEstimator(_get_xgb_classifier(model_cfg, random_state))
    cal_b = CalibratedClassifierCV(wrap_b, method="isotonic", cv=folds_b)
    cal_b.fit(X_tr, y_tr)
    p_b = cal_b.predict_proba(X_oo)[:, 1]
    ece_b, _ = compute_ece(y_oo, p_b)
    results["isotonic (3-fold)"] = {
        "ece": ece_b,
        "brier": compute_brier_score(y_oo, p_b),
        **sharpness(p_b),
        "cov55": float((p_b >= 0.55).mean()),
        "cov60": float((p_b >= 0.60).mean()),
        "a_": float("nan"),
    }

    print(f"\n{'arm':<22}{'ECE':>8}{'Brier':>8}{'std_p':>8}{'|p-.5|':>8}{'cov55':>8}{'cov60':>8}  sigmoid a_")
    for name, r in results.items():
        print(f"{name:<22}{r['ece']:>8.4f}{r['brier']:>8.4f}{r['std_p']:>8.4f}"
              f"{r['mean_abs_dev_05']:>8.4f}{r['cov55']:>8.3f}{r['cov60']:>8.3f}  {r['a_']:.4f}")

    s, i = results["sigmoid (prod)"], results["isotonic (3-fold)"]
    print("\n=== verdict ===")
    print(f"ECE    : sigmoid {s['ece']:.4f} vs isotonic {i['ece']:.4f} -> "
          f"{'isotonic better' if i['ece'] < s['ece'] else 'sigmoid better'}")
    print(f"Brier  : sigmoid {s['brier']:.4f} vs isotonic {i['brier']:.4f}")
    print(f"std_p  : sigmoid {s['std_p']:.4f} vs isotonic {i['std_p']:.4f} "
          f"-> {'isotonic sharper' if i['std_p'] > s['std_p'] else 'sigmoid sharper'}")
    print(f"cov55  : sigmoid {s['cov55']:.3f} vs isotonic {i['cov55']:.3f}")
    print(f"cov60  : sigmoid {s['cov60']:.3f} vs isotonic {i['cov60']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
