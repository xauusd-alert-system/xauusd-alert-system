"""
Layer 1 validation - isolate the effect of EACH Layer 1 change on the same data.

Layer 0 answered "are the features causal now". This answers a different
question: how much of the AUC 0.6019 -> 0.5196 collapse was the honest feature
fix, and how much was two independent defects in the training path that were
never the features' fault at all.

Three models are fitted on the SAME feature matrix and scored on the SAME test
rows, so the differences are attributable:

  A  master        plain split          + calibration capped at 100 rows
  B  +holdout      plain split          + share-based calibration holdout
  C  +purge        purged split         + share-based calibration holdout

A reproduces the numbers from the Layer 0 run. B changes ONLY the calibration
holdout size. C additionally removes the label-horizon overlap between train
and test. The test slice is byte-identical in all three.

Usage (from the repository root, venv active):

    python scripts/layer1_validation.py --symbol XAUUSD --timeframe M15 \\
        --out output/layer1_validation.txt

Nothing is deployed and no model file is written unless --save-models is given.
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import numpy as np
import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def hr(title=""):
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def git_state():
    try:
        br = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
        return f"{br} @ {sha}" + (" (dirty)" if dirty else "")
    except Exception as exc:
        return f"unavailable ({exc})"


def snapshot_db(src, no_copy=False):
    """Read from a consistent copy so a running ingester cannot shift the data."""
    if no_copy:
        print(f"read directly : {src} (--no-db-copy)")
        return src, None
    size_mb = os.path.getsize(src) / 1e6
    tmpdir = tempfile.mkdtemp(prefix="layer1_db_")
    dst = os.path.join(tmpdir, os.path.basename(src))
    try:
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
            s.backup(d)
        how = "sqlite backup api"
    except Exception as exc:
        shutil.copy2(src, dst)
        how = f"copy2 fallback ({exc})"
    print(f"read from copy: {dst}")
    print(f"                {size_mb:.1f} MB via {how}; original untouched")
    return dst, tmpdir


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_ece(y_true, y_prob, n_bins=10):
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    total, n = 0.0, len(y_true)
    if n == 0:
        return float("nan")
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (y_prob > lo) & (y_prob <= hi) if lo > 0 else (y_prob >= lo) & (y_prob <= hi)
        if not m.any():
            continue
        total += (m.sum() / n) * abs(y_true[m].mean() - y_prob[m].mean())
    return float(total)


def score_block(name, y_true, prob):
    from sklearn.metrics import roc_auc_score, brier_score_loss
    y = np.asarray(y_true, dtype=int)
    p = np.asarray(prob, dtype=float)
    return {
        "name": name,
        "auc": float(roc_auc_score(y, p)) if len(np.unique(y)) > 1 else float("nan"),
        "accuracy": float(((p >= 0.5).astype(int) == y).mean()),
        "brier": float(brier_score_loss(y, p)),
        "ece": compute_ece(y, p),
        "p_mean": float(p.mean()),
        "p_std": float(p.std()),
    }


# ---------------------------------------------------------------------------
# Calibration variants (self-contained so the script works pre- and post-patch)
# ---------------------------------------------------------------------------
def calibrate_with(base_template, X_train, y_train, cfg, min_fit, min_calib, horizon):
    """Replicates calibrate_model's purged prefit split with explicit sizes."""
    from sklearn.calibration import CalibratedClassifierCV
    from model.trainer import _fit_classifier

    method = cfg["model"]["calibration_method"]
    n = len(X_train)
    if n < min_fit + horizon + min_calib + 1:
        print(f"    !! too small for a purged calibration split "
              f"(n={n} < fit={min_fit}+purge={horizon}+calib={min_calib}); identity")
        return _fit_classifier(X_train, y_train, cfg), 0, 0
    fit_end = max(min_fit, n - horizon - min_calib)
    calib_rows = n - (fit_end + horizon)
    full_classes = set(pd.Series(y_train).unique())
    y_fit = y_train.iloc[:fit_end]
    y_cal = y_train.iloc[fit_end + horizon:]
    if set(y_fit.unique()) != full_classes or set(y_cal.unique()) != full_classes:
        print("    !! a slice lost a class; identity calibration")
        return _fit_classifier(X_train, y_train, cfg), 0, 0
    split = [(np.arange(fit_end), np.arange(fit_end + horizon, n))]
    cal = CalibratedClassifierCV(base_template, method=method, cv=split)
    cal.fit(X_train, y_train)
    return cal, fit_end, calib_rows


def plain_split(X, y, ratio):
    i = int(len(X) * ratio)
    return X.iloc[:i], X.iloc[i:], y.iloc[:i], y.iloc[i:]


def purged_split(X, y, ratio, horizon, embargo):
    i = int(len(X) * ratio)
    gap = max(0, int(horizon)) + max(0, int(embargo))
    end = max(0, i - gap)
    return X.iloc[:end], X.iloc[i:], y.iloc[:end], y.iloc[i:]


def main():
    ap = argparse.ArgumentParser(description="Layer 1 A/B/C validation")
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--symbol", default="XAUUSD", help="symbol as stored in the DB")
    ap.add_argument("--asset-key", default=None, help="key in config assets (default: --symbol)")
    ap.add_argument("--timeframe", default="M15")
    ap.add_argument("--max-bars", type=int, default=0, help="0 = all")
    ap.add_argument("--embargo", type=int, default=None,
                    help="override backtest.walk_forward.embargo_candles for variant C")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-db-copy", action="store_true")
    ap.add_argument("--save-models", action="store_true")
    ap.add_argument("--model-dir", default="output/models/layer1")
    args = ap.parse_args()

    out_fh = None
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        out_fh = open(args.out, "w", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, out_fh)

    from config.loader import load_config
    from data.storage import read_candles
    from model.trainer import build_training_matrix, _normalize_label_space, train_model

    cfg = load_config()
    asset_key = args.asset_key or args.symbol
    db_path = args.db_path or cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    if not os.path.isabs(db_path):
        db_path = os.path.join(REPO_ROOT, db_path)

    hr("LAYER 1 VALIDATION")
    print(f"generated     : {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC")
    print(f"git           : {git_state()}")
    print(f"symbol        : {args.symbol} (asset key {asset_key})  timeframe {args.timeframe}")
    if not os.path.exists(db_path):
        print(f"FATAL: {db_path} does not exist.")
        return 2
    db_path, tmpdir = snapshot_db(db_path, args.no_db_copy)

    try:
        hr("PHASE 1  build the dataset (identical for every variant)")
        raw = read_candles(db_path, args.timeframe, args.symbol)
        if raw.empty:
            print(f"FATAL: no candles for {args.symbol} {args.timeframe}.")
            return 2
        if args.max_bars and len(raw) > args.max_bars:
            raw = raw.iloc[-args.max_bars:].reset_index(drop=True)
        print(f"raw candles   : {len(raw)}")

        try:
            from scripts.train_mt5 import build_full_df
        except ImportError as exc:
            print(f"FATAL: cannot import scripts.train_mt5 ({exc}).")
            print("       Run apply_layer1.py first, or run this from the repo root.")
            return 2

        df = build_full_df(raw, cfg, db_path=db_path, asset_key=asset_key,
                           timeframe=args.timeframe)
        X, y, cols = build_training_matrix(df, cfg=cfg)
        y, _ = _normalize_label_space(y, cfg)
        print(f"labeled rows  : {len(X)}   features: {len(cols)}")
        print(f"class counts  : {y.value_counts().to_dict()}")
        if len(X) < 500:
            print("FATAL: fewer than 500 labeled rows.")
            return 2

        ratio = cfg["model"].get("train_ratio", 0.8)
        horizon = int(cfg.get("labeling", {}).get("horizon_candles_n", 0))
        embargo = args.embargo if args.embargo is not None else int(
            cfg.get("backtest", {}).get("walk_forward", {}).get("embargo_candles", 0))
        print(f"train_ratio   : {ratio}   horizon: {horizon}   embargo: {embargo}")

        Xtr_p, Xte, ytr_p, yte = plain_split(X, y, ratio)
        Xtr_g, Xte2, ytr_g, yte2 = purged_split(X, y, ratio, horizon, embargo)
        assert len(Xte) == len(Xte2) and (Xte.index == Xte2.index).all(), \
            "test slices must be identical across variants"
        base_rate = float(np.asarray(yte).mean())
        print(f"train (plain) : {len(Xtr_p)}    train (purged): {len(Xtr_g)}"
              f"    dropped: {len(Xtr_p) - len(Xtr_g)}")
        print(f"test rows     : {len(Xte)}   base rate (P(y=1)): {base_rate:.4f}")

        hr("PHASE 2  fit the three variants")
        variants = []
        specs = [
            ("A master", Xtr_p, ytr_p, max(30, min(200, len(Xtr_p) // 3 // 2)),
             max(20, min(100, len(Xtr_p) // 6 // 2))),
            ("B +holdout", Xtr_p, ytr_p, max(30, int(len(Xtr_p) * 0.60)),
             max(20, int(len(Xtr_p) * 0.15))),
            ("C +purge", Xtr_g, ytr_g, max(30, int(len(Xtr_g) * 0.60)),
             max(20, int(len(Xtr_g) * 0.15))),
        ]
        for name, Xt, yt, mf, mc in specs:
            print(f"\n  {name}: train_rows={len(Xt)} min_fit={mf} min_calib={mc}")
            base = train_model(Xt, yt, cfg)
            model, fit_rows, calib_rows = calibrate_with(base, Xt, yt, cfg, mf, mc, horizon)
            print(f"    fit_rows={fit_rows} calib_rows={calib_rows} purge_gap={horizon}")
            pos = int(max(getattr(model, "classes_", [0, 1])))
            idx = list(getattr(model, "classes_", [0, 1])).index(pos)
            prob = model.predict_proba(Xte)[:, idx]
            variants.append((name, model, prob))
            if args.save_models:
                from model.trainer import save_model
                os.makedirs(args.model_dir, exist_ok=True)
                tag = name.split()[0]
                save_model(model, cols, os.path.join(args.model_dir, f"variant_{tag}.joblib"))

        hr("PHASE 3a  headline metrics on the identical test slice")
        rows = [score_block(n, yte, p) for n, _m, p in variants]
        print(f"{'variant':<12} {'auc':>8} {'accuracy':>9} {'brier':>8} {'ece':>8} "
              f"{'p_mean':>8} {'p_std':>8}")
        print("-" * 68)
        for r in rows:
            print(f"{r['name']:<12} {r['auc']:>8.4f} {r['accuracy']:>9.4f} "
                  f"{r['brier']:>8.4f} {r['ece']:>8.4f} {r['p_mean']:>8.4f} {r['p_std']:>8.4f}")
        print(f"\nbase rate = {base_rate:.4f}  (accuracy at or below this = no skill)")
        a, b, c = rows
        print(f"\ncalibration holdout alone (A->B): "
              f"acc {b['accuracy'] - a['accuracy']:+.4f}  ece {b['ece'] - a['ece']:+.4f}  "
              f"brier {b['brier'] - a['brier']:+.4f}  auc {b['auc'] - a['auc']:+.4f}")
        print(f"purged split alone        (B->C): "
              f"acc {c['accuracy'] - b['accuracy']:+.4f}  ece {c['ece'] - b['ece']:+.4f}  "
              f"brier {c['brier'] - b['brier']:+.4f}  auc {c['auc'] - b['auc']:+.4f}")

        hr("PHASE 3b  precision / coverage at the live alert thresholds")
        ens = cfg.get("ensemble", {})
        acfg = cfg.get("assets", {}).get(asset_key, {})
        thresholds = [
            ("min_ml_probability", float(ens.get("min_ml_probability", 0.55))),
            ("ml_confidence_floor", float(ens.get("ml_confidence_floor", 0.62))),
            ("min_confidence_to_alert", float(acfg.get("min_confidence_to_alert",
                                                       ens.get("min_confidence_to_alert", 0.60)))),
        ]
        yt_arr = np.asarray(yte, dtype=int)
        for label, thr in thresholds:
            print(f"\n  {label} = {thr}")
            print(f"    {'variant':<12} {'long prec':>10} {'long cov':>9} "
                  f"{'short prec':>11} {'short cov':>10}")
            for name, _m, p in variants:
                lm = p >= thr
                sm = p <= (1.0 - thr)
                lp = float(yt_arr[lm].mean()) if lm.any() else float("nan")
                sp = float((yt_arr[sm] == 0).mean()) if sm.any() else float("nan")
                print(f"    {name:<12} {lp:>10.4f} {lm.mean():>9.4f} "
                      f"{sp:>11.4f} {sm.mean():>10.4f}")

        hr("CAVEATS")
        print("* One time-ordered split, not a walk-forward. Treat the numbers as a")
        print("  relative comparison between variants, not as deployable performance.")
        print("* Variant C removes only train rows; the test slice is identical, so the")
        print("  A/B/C differences are attributable to the changes and nothing else.")
        print("* No model here is fit for deployment. Walk-forward first.")
        return 0
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if out_fh:
            sys.stdout = sys.__stdout__
            out_fh.close()
            print(f"\nwrote {args.out}")


if __name__ == "__main__":
    sys.exit(main())
