"""Why do XAUUSD production probabilities sit in [0.5, 0.6] for ~95% of bars?

Follows the probability pipeline stage by stage on true-UTC data:

  1. LABEL QUALITY   - labeling.event='traded' base rates (traded_event_summary)
                       + the actual training-label class balance. If both sides
                       resolve favourably almost always, the direction subset is
                       tiny and near-random -> nothing for the model to learn.
  2. RAW MARGINS     - decision_function of the fitted XGBoost inside
                       CalibratedClassifierCV (log-odds space): spread, AUC
                       against the label.
  3. CALIBRATION MAP - the fitted Platt sigmoid a/b: which margin window maps
                       into p in [0.5, 0.6], and how much raw spread survives.
  4. FINAL PROBS     - ModelPredictor output distribution (the numbers the
                       dashboard / gates see), fraction inside [0.5, 0.6].

Usage:
    python -m scripts.diag_xau_prob_collapse [--end-date 2026-08-08]
"""
import argparse
import os
import sys

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from labeling.label_generator import (
    generate_labels_from_config,
    traded_event_summary,
)
from model.predictor import ModelPredictor
from scripts.run_backtest import load_asset_history, truncate_before
import scripts.train_mt5 as train_mt5

MODEL_PATH = "output/models/xauusd_direction_model.joblib"


def _fitted_base_estimator(cc):
    """Pull one FITTED base estimator out of CalibratedClassifierCV."""
    cc0 = cc.calibrated_classifiers_[0]
    est = getattr(cc0, "estimator", None) or getattr(cc0, "clf", None)
    if est is None or not hasattr(est, "feature_importances_") and not hasattr(est, "predict"):
        raise RuntimeError("cannot locate fitted base estimator inside bundle")
    return est


def _sigmoid_map(f: np.ndarray, a: float, b: float) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(a * f + b))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end-date", default="2026-08-08")
    args = ap.parse_args(argv)

    cfg = load_config()
    db = cfg["general"]["db_path"]
    asset = "XAUUSD"
    timeframe = cfg["assets"][asset].get("timeframe", "M15")

    print("loading true-UTC history ...", flush=True)
    raw = load_asset_history(db, timeframe, asset)
    raw = truncate_before(raw, args.end_date, asset)
    df = train_mt5.build_full_df(raw, cfg, db_path=db, asset_key=asset, timeframe=timeframe)
    print(f"featured rows: {len(df)}  {df['timestamp'].min()} .. {df['timestamp'].max()}", flush=True)

    # ------------------------------------------------------------------ #
    print("\n===== 1. LABEL QUALITY (labeling.event='traded') =====")
    summ = traded_event_summary(df, cfg, asset_key=asset)
    for k, v in summ.items():
        print(f"  {k:26s} = {v}")
    lab = generate_labels_from_config(df, cfg, asset_key=asset)
    vc = lab.value_counts(dropna=False)
    n_valid = int(vc.sum())
    print(f"  label balance: long={vc.get(1.0, 0)} ({vc.get(1.0, 0)/n_valid*100:.1f}%) "
          f"short={vc.get(-1.0, vc.get(0.0, 0))} nan={int(lab.isna().sum())}")

    # ------------------------------------------------------------------ #
    print("\n===== 2. RAW XGBOOST MARGINS (log-odds) =====")
    bundle = joblib.load(MODEL_PATH)
    cc = bundle["model"]
    feats = list(bundle["feature_cols"])
    missing = [c for c in feats if c not in df.columns]
    if missing:
        raise SystemExit(f"featured frame lacks columns: {missing[:8]} ...")
    X = df[feats].astype(float)
    valid_mask = ~X.isnull().any(axis=1) & lab.notna().values
    Xv = X[valid_mask]
    y = lab[valid_mask].values
    y01 = (y == 1.0).astype(int)

    base = _fitted_base_estimator(cc)
    # This xgboost build exposes no decision_function; its own predict_proba IS
    # sigmoid(raw margin), so recover the log-odds margin exactly.
    raw_p1 = np.asarray(base.predict_proba(Xv))[:, 1]
    raw_p1 = np.clip(raw_p1, 1e-9, 1 - 1e-9)
    margin = np.log(raw_p1 / (1 - raw_p1))
    qs = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
    print("  margin percentiles:", {q: round(float(np.quantile(margin, q)), 3) for q in qs})
    print(f"  std={margin.std():.4f}  IQR={np.quantile(margin,.75)-np.quantile(margin,.25):.4f}")

    from sklearn.metrics import roc_auc_score
    auc = roc_auc_score(y01, margin)
    print(f"  AUC(raw margin vs traded-direction label) = {auc:.4f}")

    # ------------------------------------------------------------------ #
    print("\n===== 3. CALIBRATION TRANSFER =====")
    cal = cc.calibrated_classifiers_[0]
    cb = getattr(cal, "calibrators", None) or getattr(cal, "calibrator", None)
    cb0 = cb[0] if isinstance(cb, list) else cb
    a_, b_ = float(cb0.a_), float(cb0.b_)
    print(f"  Platt sigmoid: a={a_:.4f} b={b_:.4f}")
    # sklearn feeds the calibrator the base estimator's predict_proba[:,1]
    # (this xgboost exposes no decision_function), i.e. the input space is
    # RAW PROBABILITY [0,1], not log-odds. Verify both hypotheses:
    direct = cc.predict_proba(Xv)[:, list(cc.classes_).index(1)]
    via_p = _sigmoid_map(raw_p1, a_, b_)
    via_f = _sigmoid_map(margin, a_, b_)
    print(f"  max |sigmoid(raw_p) - predict_proba|   = {np.abs(via_p - direct).max():.2e}  <- correct space")
    print(f"  max |sigmoid(logit) - predict_proba|   = {np.abs(via_f - direct).max():.2e}")
    # local slope of the transfer at p_raw=0.5 (1.0 = identity)
    d = 0.005
    slope = (_sigmoid_map(0.5 + d, a_, b_) - _sigmoid_map(0.5 - d, a_, b_)) / (2 * d)
    print(f"  transfer slope at p_raw=0.5: {slope:.3f}  (<1 = compression toward 0.5)")
    # what raw probability each alert gate requires
    for gate in (0.62, 0.66, 0.71):
        need = (np.log(1 / gate - 1) - b_) / a_
        print(f"  p_cal>={gate}: needs raw_p >= {need:.3f} -> {(raw_p1 >= need).mean()*100:.2f}% of bars")

    # ------------------------------------------------------------------ #
    print("\n===== 4. FINAL PROBABILITIES (production predictor) =====")
    pred = ModelPredictor(MODEL_PATH)
    probs = pred.predict_proba(df.loc[Xv.index])
    p_long, p_short = probs["p_long"].values, probs["p_short"].values
    p_max = np.maximum(p_long, p_short)
    for name, arr in (("p_long", p_long), ("p_max", p_max)):
        print(f"  {name}: " + str({q: round(float(np.quantile(arr, q)), 3) for q in qs}))
    for lo, hi in ((0.5, 0.6), (0.45, 0.65)):
        share = float(((p_max >= lo) & (p_max <= hi)).mean())
        print(f"  P(p_max in [{lo},{hi}]) = {share*100:.1f}%")
    # recency slice: last 90 days of the sample
    ts_idx = df.loc[Xv.index, "timestamp"]
    cutoff = ts_idx.max() - pd.Timedelta(days=90)
    recent = ts_idx >= cutoff
    print(f"  last-90d: P(p_max in [0.5,0.6]) = "
          f"{float(((p_max[recent.values] >= 0.5) & (p_max[recent.values] <= 0.6)).mean())*100:.1f}%"
          f" (n={int(recent.sum())})")


if __name__ == "__main__":
    main()
