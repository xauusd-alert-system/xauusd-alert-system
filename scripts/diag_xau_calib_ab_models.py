"""Calibration A/B: every preserved XAUUSD model artifact scored on the SAME
true-UTC out-of-sample window, against the SAME labels.

Candidates are discovered on disk (current production bundle + every preserved
pre-fix variant). For each we report, on one common window:

  - discrimination: AUC(raw p_long vs traded-direction label)
  - calibration:    Brier score (vs climatology), ECE over 10 equal-count bins,
                    decile reliability table (mean predicted vs observed)
  - sharpness:      std(p_long), share of p_max inside [0.5, 0.6] (the collapse
                    metric from diag_xau_prob_collapse)
  - gate coverage:  share of bars with p_long/p_short >= 0.62 / 0.66

CAVEAT printed with the results: the window is strictly out-of-sample for the
older artifacts but partially IN-SAMPLE for the current production model if it
was retrained over data extending into the window. In-sample advantage shows up
as sharper probabilities, so treat a current-model win on Brier/ECE with care;
a current-model LOSS is still informative.

Usage:
    python -m scripts.diag_xau_calib_ab_models [--window-start 2026-05-01]
                                               [--end-date 2026-08-08]
"""
import argparse
import os
import sys

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from labeling.label_generator import generate_labels_from_config
from scripts.run_backtest import load_asset_history, truncate_before
import scripts.train_mt5 as train_mt5

CANDIDATES = [
    ("CURRENT(Aug26)", "output/models/xauusd_direction_model.joblib"),
    ("pre_calib_fix", "output/models/xauusd_direction_model.pre_calib_fix_20260825.joblib"),
    ("PRE_trueUTC", "logs/xauusd_model_PRE_trueUTC_retrain.joblib"),
    ("backup_pre_layer0", "output/models/backup_pre_layer0_fix/xauusd_direction_model.joblib"),
]


def _ece(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> float:
    """Equal-count bin ECE: |mean(p) - obs_freq| averaged with bin weights."""
    order = np.argsort(p)
    bins = np.array_split(order, n_bins)
    err, n = 0.0, len(y)
    for b in bins:
        if len(b) == 0:
            continue
        err += abs(p[b].mean() - y[b].mean()) * len(b) / n
    return float(err)


def _reliability_rows(y: np.ndarray, p: np.ndarray) -> list[tuple[str, int, float, float]]:
    edges = [(0.40, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 0.70), (0.70, 1.01)]
    rows = []
    for lo, hi in edges:
        m = (p >= lo) & (p < hi)
        if m.sum() == 0:
            rows.append((f"[{lo:.2f},{hi:.2f})", 0, float("nan"), float("nan")))
        else:
            rows.append((f"[{lo:.2f},{hi:.2f})", int(m.sum()), round(float(p[m].mean()), 3),
                         round(float(y[m].mean()), 3)))
    return rows


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end-date", default="2026-08-08")
    ap.add_argument("--window-start", default="2026-05-01",
                    help="OOS evaluation window start (UTC)")
    args = ap.parse_args(argv)

    cfg = load_config()
    db = cfg["general"]["db_path"]
    asset = "XAUUSD"
    timeframe = cfg["assets"][asset].get("timeframe", "M15")

    print("loading true-UTC history ...", flush=True)
    raw = truncate_before(load_asset_history(db, timeframe, asset), args.end_date, asset)
    df = train_mt5.build_full_df(raw, cfg, db_path=db, asset_key=asset, timeframe=timeframe)
    lab = generate_labels_from_config(df, cfg, asset_key=asset)

    # Load all bundles first; drop unloadable ones loudly.
    bundles = []
    for name, path in CANDIDATES:
        try:
            bundles.append((name, joblib.load(path)))
            print(f"  loaded {name}: {len(bundles[-1][1]['feature_cols'])} feats")
        except Exception as e:
            print(f"  SKIP {name} ({path}): {type(e).__name__}: {e}")
    if not bundles:
        raise SystemExit("no candidate model could be loaded")

    # Common row set: inside window, label present, NaN-free for EVERY bundle.
    ts = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    win = (ts >= pd.Timestamp(args.window_start)) & (ts < pd.Timestamp(args.end_date))
    feat_union = sorted({c for _, b in bundles for c in b["feature_cols"]})
    missing = [c for c in feat_union if c not in df.columns]
    if missing:
        raise SystemExit(f"featured frame lacks columns: {missing[:10]} ...")
    valid = win.values & lab.notna().values & ~df[feat_union].isnull().any(axis=1).values
    sub = df.loc[valid]
    y01 = (lab[valid].values == 1.0).astype(int)
    print(f"\nwindow {args.window_start} .. {args.end_date}: n={valid.sum()} "
          f"(base rate long={y01.mean()*100:.1f}%)")

    rows = []
    for name, bundle in bundles:
        feats = list(bundle["feature_cols"])
        X = sub[feats].astype(float)
        pred_p = bundle["model"].predict_proba(X)[:, list(bundle["model"].classes_).index(1)]
        p_long = np.asarray(pred_p, dtype=float)
        p_max = np.maximum(p_long, 1.0 - p_long)
        base_rate = y01.mean()
        brier = float(np.mean((p_long - y01) ** 2))
        brier_clim = float(np.mean((base_rate - y01) ** 2))
        auc = float(pd.DataFrame({"p": p_long, "y": y01}).dropna().pipe(
            lambda d: __import__("sklearn.metrics", fromlist=["roc_auc_score"]).roc_auc_score(d["y"], d["p"])
        ))
        rows.append({
            "model": name,
            "AUC": round(auc, 4),
            "Brier": round(brier, 5),
            "Brier_clim": round(brier_clim, 5),
            "ECE10": round(_ece(y01, p_long), 4),
            "std_p": round(float(p_long.std()), 4),
            "pmax_[.5-.6]_%": round(float(((p_max >= 0.5) & (p_max <= 0.6)).mean()) * 100, 1),
            "cov>=.62_%": round(float((p_max >= 0.62).mean()) * 100, 2),
            "cov>=.66_%": round(float((p_max >= 0.66).mean()) * 100, 2),
            "long_cov>=.66_%": round(float((p_long >= 0.66).mean()) * 100, 2),
            "short_cov>=.66_%": round(float((p_long <= 0.34).mean()) * 100, 2),
        })

    summary = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print("\n========== CALIBRATION A/B (same window, same labels) ==========")
    print(summary.to_string(index=False))

    for name, bundle in bundles:
        p_long = np.asarray(
            bundle["model"].predict_proba(sub[list(bundle["feature_cols"])].astype(float))[
                :, list(bundle["model"].classes_).index(1)], dtype=float)
        print(f"\n--- reliability: {name}")
        for rng, n, mp, oy in _reliability_rows(y01, p_long):
            gap = "" if n == 0 else f"gap={mp - oy:+.3f}"
            print(f"  {rng:12s} n={n:6d} mean_p={mp:.3f} observed={oy:.3f} {gap}")

    print(f"""
NOTE: window is OOS for the preserved artifacts (trained <= Aug 24) but may be
partially IN-SAMPLE for CURRENT(Aug26) (retrained over data ending near the
window end). In-sample advantage inflates sharpness/AUC; read CURRENT wins on
Brier/ECE accordingly.""", flush=True)


if __name__ == "__main__":
    main()
