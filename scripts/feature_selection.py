"""
Feature selection via MDA on purged CV (quant audit 2026-08-07, Claude plan
action 2): reduce the 46 features to 12-15 for the low-event assets.

Why NOT `feature_importances_`: XGBoost's built-in importances are biased
toward high-cardinality / redundant features. The audit prescribes:

- MDA (mean decrease accuracy) with purged K-fold + embargo (model/cv.py),
  where each feature is permuted within the TEST block and the OOS accuracy
  drop is the importance;
- cluster correlated features (|rho| >= threshold) and pick the best member
  per cluster (or top-K overall), so the final set has ~one representative
  per information family (trend / vol / position-in-range / MTF / order-flow /
  time).

Outputs logs/feature_selection_<asset>.json with per-feature MDA ranks and
the suggested subset; optionally writes a config snippet
(config/feature_subsets/<asset>.yaml) that can be enabled per-asset via
model.feature_subset (supported by build_training_matrix + ModelPredictor).

Honesty: MDA is computed on the TRAIN part of the walk-forward span only
(the same data the per-fold models see); nothing touches the test windows.

Usage:
    python -m scripts.feature_selection --asset GBPUSD --max-features 15
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import effective_asset_config, load_config
from model.cv import purged_kfold_indices
from model.trainer import FEATURE_COLUMNS, build_training_matrix, train_model
from scripts.deflated_sharpe import (
    _SYNTH_DEFAULTS,
    _inject_biased_probs,
    _make_synthetic_wf_df,
)
from scripts.run_backtest import merge_asset_cfg


def _oof_accuracy(model, X_test: pd.DataFrame, y_test: pd.Series) -> float:
    preds = model.predict(X_test)
    return float(np.mean(preds == y_test.to_numpy()))


def mda_feature_importance(df: pd.DataFrame, cfg: dict, asset_key: str = "XAUUSD",
                           n_splits: int = 4, n_permute: int = 10,
                           random_seed: int = 42) -> pd.DataFrame:
    """Mean-decrease-accuracy importance on purged K-fold.

    Returns a DataFrame indexed by feature with columns: mda (mean drop in
    OOS accuracy across folds and permutations), mda_std, fold_count.
    """
    model_cfg = merge_asset_cfg(cfg, asset_key, "model")["model"]
    X, y, cols = build_training_matrix(df, cfg={"model": model_cfg})
    if len(y) < 40:
        raise ValueError(f"Too few labeled rows for feature selection ({len(y)}).")
    horizon = int(merge_asset_cfg(cfg, asset_key, "labeling")["labeling"].get(
        "horizon_candles_n", 36))
    folds = purged_kfold_indices(len(df), n_splits=n_splits, horizon=horizon, embargo=0)
    # map row positions of the labeled matrix back to df positions: build_training
    # matrix drops NaN rows, so align by index values.
    idx_pos = {pos: i for i, pos in enumerate(X.index)}

    rng = np.random.default_rng(random_seed)
    drops = {c: [] for c in cols}
    for fold_i, (tr_pos, te_pos) in enumerate(folds):
        tr = [idx_pos[p] for p in tr_pos if p in idx_pos]
        te = [idx_pos[p] for p in te_pos if p in idx_pos]
        if len(tr) < 30 or len(te) < 5:
            continue
        X_tr, y_tr = X.iloc[tr], y.iloc[tr]
        X_te, y_te = X.iloc[te], y.iloc[te]
        if y_tr.nunique() < 2 or y_te.nunique() < 2:
            continue
        model = train_model(X_tr, y_tr, {"model": model_cfg})
        base_acc = _oof_accuracy(model, X_te, y_te)
        for c in cols:
            for _ in range(n_permute):
                X_perm = X_te.copy()
                X_perm[c] = X_perm[c].sample(frac=1.0, random_state=rng.integers(0, 1 << 30)).values
                drops[c].append(base_acc - _oof_accuracy(model, X_perm, y_te))

    if not drops or all(len(v) == 0 for v in drops.values()):
        raise ValueError("No valid folds for MDA (too few rows after purge).")
    rows = []
    for c, vals in drops.items():
        if not vals:
            continue
        arr = np.asarray(vals, dtype=float)
        rows.append({"feature": c, "mda": float(arr.mean()), "mda_std": float(arr.std(ddof=1))})
    return pd.DataFrame(rows).sort_values("mda", ascending=False).reset_index(drop=True)


def cluster_correlated_features(df: pd.DataFrame, mda_df: pd.DataFrame,
                                threshold: float = 0.90) -> pd.DataFrame:
    """Greedy clustering by |Pearson rho| >= threshold; each cluster keeps the
    member with the highest MDA. Returns the reduced set (one per cluster)."""
    feats = mda_df["feature"].tolist()
    X = df[feats].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    corr = X.corr().abs()
    picked = []
    used = set()
    for f in feats:  # already sorted by MDA descending
        if f in used:
            continue
        cluster = [f] + [g for g in feats
                         if g not in used and g != f
                         and np.isfinite(corr.loc[f, g]) and corr.loc[f, g] >= threshold]
        picked.append(f)
        used.update(cluster)
    return mda_df[mda_df["feature"].isin(picked)].reset_index(drop=True)


def suggest_subset(mda_df: pd.DataFrame, max_features: int = 15) -> list[str]:
    """Top-K by MDA, capped at max_features (empty-tolerant)."""
    return mda_df["feature"].head(max_features).tolist()


def run_feature_selection(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                          max_features: int = 15, corr_threshold: float = 0.90,
                          n_splits: int = 4, n_permute: int = 10) -> dict:
    """Full pipeline: MDA ranks + clustered reduction + top-K subset."""
    mda_df = mda_feature_importance(df_full, cfg, asset_key=asset_key,
                                   n_splits=n_splits, n_permute=n_permute)
    # The MDA set may include regime_* one-hots that live only inside the
    # training matrix; materialize them on the working frame for clustering.
    df_work = df_full
    if "regime" in df_full.columns:
        from regime.classifier import regime_onehot_df
        oh = regime_onehot_df(df_full)
        new_cols = [c for c in oh.columns if c not in df_work.columns]
        if new_cols:
            df_work = pd.concat([df_work, oh[new_cols]], axis=1)
    clustered = cluster_correlated_features(df_work, mda_df, threshold=corr_threshold)
    subset = suggest_subset(clustered, max_features=max_features)
    return {
        "asset": asset_key,
        "n_features_total": len(FEATURE_COLUMNS),
        "mda_rank": mda_df.to_dict(orient="records"),
        "clustered": clustered["feature"].tolist(),
        "suggested_subset": subset,
        "n_suggested": len(subset),
    }


def print_report(d: dict) -> None:
    print(f"\n=== Feature selection (MDA on purged CV): {d['asset']} ===")
    print(f"Total features: {d['n_features_total']} -> suggested subset: {d['n_suggested']}")
    print("MDA rank (top 20):")
    for i, r in enumerate(d["mda_rank"][:20]):
        print(f"  {i + 1:>2}. {r['feature']:<28} mda={r['mda']:+.4f} ± {r['mda_std']:.4f}")
    print(f"Clustered representatives ({len(d['clustered'])}): {d['clustered']}")
    print(f"Suggested subset ({d['n_suggested']}): {d['suggested_subset']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MDA feature selection on purged CV.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-folds", type=int, default=None, help="Cap folds (quick runs/tests)")
    parser.add_argument("--max-features", type=int, default=15)
    parser.add_argument("--corr-threshold", type=float, default=0.90)
    parser.add_argument("--n-splits", type=int, default=4)
    parser.add_argument("--out", default=None, help="JSON output (default: logs/feature_selection_<asset>.json)")
    args = parser.parse_args(argv)

    cfg = load_config()
    assets = cfg.get("assets", {})
    if args.asset not in assets:
        raise SystemExit(f"Unknown asset: {args.asset}")
    asset_cfg = assets[args.asset]
    timeframe = args.timeframe or asset_cfg.get("timeframe") or "M5"
    db_path = args.db_path or cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    synthetic = False
    try:
        from scripts.run_backtest import build_full_df, load_asset_history
        raw = load_asset_history(db_path, timeframe, args.asset)
        df = build_full_df(cfg, raw, db_path=db_path, asset_key=args.asset)
        print(f"[featsel] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(f"[featsel] WARNING: cannot load real data ({exc.__class__.__name__}); "
              "SYNTHETIC demo — results are NOT real.")
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)
        from labeling.label_generator import generate_labels_from_config
        cfg_asset = effective_asset_config(cfg, args.asset)
        df["label"] = generate_labels_from_config(
            df, cfg_asset, asset_key=args.asset
        )

    d = run_feature_selection(cfg, args.asset, df, max_features=args.max_features,
                              corr_threshold=args.corr_threshold,
                              n_splits=args.n_splits, n_permute=5)
    d["synthetic"] = synthetic
    print_report(d)

    os.makedirs("logs", exist_ok=True)
    out_json = args.out or f"logs/feature_selection_{args.asset.lower()}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"[featsel] -> {out_json}")
    print("[featsel] To enable the subset per asset, set in config:")
    print(f"          assets.{args.asset}.model.feature_subset = {d['suggested_subset']}")


if __name__ == "__main__":
    main()
