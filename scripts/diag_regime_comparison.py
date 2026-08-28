"""Compare rule-based ADX/DI regime classifier vs unsupervised GMM on forward-returns.

For each asset:
  1. Load M15 candles from the production DB
  2. Build full feature set (regime indicators included)
  3. Compute rule-based regime labels (classify_regime_series)
  4. Fit unsupervised GMM (UnsupervisedRegimeClassifier) on train split, predict on test
  5. Measure per-regime forward returns (1h, 4h, 1d horizons)
  6. Compare: IC (rank correlation), per-regime mean R, dispersion, hit rate

Output: logs/regime_comparison_*.csv + console summary.
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from regime.classifier import classify_regime_series
from regime.hmm_classifier import UnsupervisedRegimeClassifier
from scripts.run_backtest import build_full_df, load_asset_history, merge_asset_cfg

ASSETS = ["XAUUSD", "BTCUSD", "EURUSD", "GBPUSD", "XAGUSD"]
FWD_HORIZONS_M15 = {"1h": 4, "4h": 16, "1d": 64}  # bars on M15
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _forward_returns(df: pd.DataFrame, horizons: dict[str, int]) -> pd.DataFrame:
    """Add forward return columns for multiple horizons."""
    out = df.copy()
    for label, n in horizons.items():
        out[f"fwd_ret_{label}"] = out["close"].shift(-n) / out["close"] - 1
    return out


def _per_regime_stats(df: pd.DataFrame, regime_col: str, ret_cols: list[str]) -> pd.DataFrame:
    """Per-regime aggregated statistics."""
    rows = []
    for regime in sorted(df[regime_col].unique()):
        sub = df[df[regime_col] == regime]
        n = len(sub)
        if n < 10:
            continue
        row = {"regime": regime, "n": n, "n_pct": round(100 * n / len(df), 1)}
        for rc in ret_cols:
            r = sub[rc].dropna()
            if len(r) < 5:
                row[f"{rc}_mean"] = None
                row[f"{rc}_std"] = None
                row[f"{rc}_sharpe"] = None
                continue
            mean = float(r.mean())
            std = float(r.std())
            row[f"{rc}_mean"] = round(mean * 10000, 2)  # bps
            row[f"{rc}_std"] = round(std * 10000, 2)
            row[f"{rc}_sharpe"] = round(mean / std * np.sqrt(252 * 64), 2) if std > 0 else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def _information_coefficient(df: pd.DataFrame, regime_col: str, ret_col: str, ordinal_map: dict | None = None) -> float:
    """Spearman rank correlation between ordinal regime and forward returns."""
    if ordinal_map is None:
        ordinal_map = {
            "trend_up": 2,
            "trend_down": -2,
            "range": 0,
            "compression": 0.5,
            "reversal_watch": -0.5,
            "no_trade": 0,
        }
    valid = df[[regime_col, ret_col]].dropna()
    if len(valid) < 30:
        return float("nan")
    ordinal = valid[regime_col].map(ordinal_map)
    if ordinal.nunique() <= 1:
        return float("nan")  # constant -> IC undefined
    return float(ordinal.corr(valid[ret_col], method="spearman"))


def _hit_rate(
    df: pd.DataFrame,
    regime_col: str,
    ret_col: str,
    bullish_regimes: set | None = None,
    bearish_regimes: set | None = None,
) -> dict:
    """Directional hit rate: does the regime 'agree' with the sign of the return?"""
    if bullish_regimes is None:
        bullish_regimes = {"trend_up", "high_vol_trending"}
    if bearish_regimes is None:
        bearish_regimes = {"trend_down", "high_vol_shock"}
    neutral_regimes = {"range", "compression", "reversal_watch", "no_trade", "standard_range", "low_vol_compression"}
    valid = df[[regime_col, ret_col]].dropna()
    hits = 0
    total = 0
    for _, r in valid.iterrows():
        reg = r[regime_col]
        ret = r[ret_col]
        if reg in bullish_regimes and ret > 0:
            hits += 1
            total += 1
        elif reg in bearish_regimes and ret < 0:
            hits += 1
            total += 1
        elif reg in neutral_regimes:
            total += 1
    return {"hits": hits, "total directional": total, "hit_rate": round(100 * hits / total, 1) if total > 0 else None}


def run_asset(asset_key: str, cfg: dict, max_bars: int | None = None) -> dict:
    """Run comparison for one asset. Returns a dict of results."""
    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    tf = asset_cfg.get("timeframe") or cfg.get("market_data", {}).get("timeframe", "M5")
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    raw = load_asset_history(db_path, tf, asset_key)
    if raw.empty:
        return {"asset": asset_key, "error": "no data"}
    if max_bars and len(raw) > max_bars:
        raw = raw.tail(max_bars).reset_index(drop=True)

    cfg_a = merge_asset_cfg(cfg, asset_key, "labeling")
    cfg_a = merge_asset_cfg(cfg_a, asset_key, "features")
    cfg_a = merge_asset_cfg(cfg_a, asset_key, "regime")

    df = build_full_df(cfg_a, raw, db_path=db_path, asset_key=asset_key)
    # Drop warm-up NaN rows
    df = df.dropna(subset=["close", "adx"]).reset_index(drop=True)

    if len(df) < 200:
        return {"asset": asset_key, "error": f"too few rows ({len(df)})"}

    # Rule-based regime
    df["regime_rule"] = classify_regime_series(df, cfg_a)

    # Unsupervised GMM: fit on first 70%, predict on last 30%
    split = int(len(df) * 0.7)
    train_df = df.iloc[:split]
    test_df = df.iloc[split:].copy()

    gmm = UnsupervisedRegimeClassifier(n_regimes=4, random_state=42)
    gmm.fit(train_df)
    test_df["regime_gmm"] = gmm.predict_regime(test_df)

    # Forward returns
    horizons = FWD_HORIZONS_M15 if tf.upper() == "M15" else {"1h": 12, "4h": 48}
    ret_cols = [f"fwd_ret_{h}" for h in horizons]

    # --- Rule-based stats (full sample, since it has no train/test split) ---
    rule_df = _forward_returns(df, horizons)
    rule_stats = _per_regime_stats(rule_df, "regime_rule", ret_cols)
    rule_ic = {rc: _information_coefficient(rule_df, "regime_rule", rc) for rc in ret_cols}
    rule_hit = {rc: _hit_rate(rule_df, "regime_rule", rc) for rc in ret_cols}
    rule_dist = rule_df["regime_rule"].value_counts(normalize=True).to_dict()

    # --- GMM stats (test set only) ---
    gmm_df = _forward_returns(test_df, horizons)
    gmm_stats = _per_regime_stats(gmm_df, "regime_gmm", ret_cols)
    # Map GMM labels to ordinal: high_vol_trending > standard_range > low_vol_compression > high_vol_shock
    gmm_ordinal = {
        "high_vol_trending": 2,
        "standard_range": 0.5,
        "low_vol_compression": -0.5,
        "high_vol_shock": -2,
    }
    gmm_ic = {rc: _information_coefficient(gmm_df, "regime_gmm", rc, gmm_ordinal) for rc in ret_cols}
    gmm_hit = {rc: _hit_rate(gmm_df, "regime_gmm", rc) for rc in ret_cols}
    gmm_dist = gmm_df["regime_gmm"].value_counts(normalize=True).to_dict()

    return {
        "asset": asset_key,
        "tf": tf,
        "n_total": len(df),
        "n_test": len(test_df),
        "rule": {
            "stats": rule_stats,
            "ic": rule_ic,
            "hit": rule_hit,
            "distribution": rule_dist,
        },
        "gmm": {
            "stats": gmm_stats,
            "ic": gmm_ic,
            "hit": gmm_hit,
            "distribution": gmm_dist,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-bars", type=int, default=50000, help="Cap rows per asset for speed")
    parser.add_argument("--assets", default=",".join(ASSETS), help="Comma-separated asset keys")
    parser.add_argument("--out-dir", default="logs")
    args = parser.parse_args()

    cfg = load_config()
    assets = [a.strip() for a in args.assets.split(",") if a.strip()]

    results = []
    for asset in assets:
        print(f"\n{'=' * 60}\n  {asset}\n{'=' * 60}")
        r = run_asset(asset, cfg, max_bars=args.max_bars)
        results.append(r)

        if "error" in r:
            print(f"  SKIP: {r['error']}")
            continue

        # --- Summary ---
        rc = "fwd_ret_4h"  # primary horizon
        print(f"\n  TF={r['tf']}  N={r['n_total']}  Test={r['n_test']}")
        print(f"\n  Distribution (rule): {r['rule']['distribution']}")
        print(f"  Distribution (GMM):  {r['gmm']['distribution']}")

        print(f"\n  {'Regime':<18} {'N%':>5}  {'Mean 4h (bps)':>13}  {'Sharpe':>8}  |  {'IC':>6}")
        print(f"  {'-' * 62}")

        # Rule-based
        print("  [Rule-based ADX/DI]")
        for _, row in r["rule"]["stats"].iterrows():
            mean = row.get(f"{rc}_mean", "—")
            sharpe = row.get(f"{rc}_sharpe", "—")
            print(f"  {row['regime']:<18} {row['n_pct']:>4.1f}%  {str(mean):>13}  {str(sharpe):>8}")
        ic_val = r["rule"]["ic"].get(rc, float("nan"))
        print(f"  {'IC(' + rc + ')':<18} {'':>5}  {'':>13}  {'':>8}  |  {ic_val:>+.4f}")
        hr = r["rule"]["hit"].get(rc, {})
        print(
            f"  {'Hit rate':<18} {'':>5}  hits={hr.get('hits', '?')}/{hr.get('total directional', '?')}  {hr.get('hit_rate', '—')}%"
        )

        # GMM
        print("\n  [Unsupervised GMM]")
        for _, row in r["gmm"]["stats"].iterrows():
            mean = row.get(f"{rc}_mean", "—")
            sharpe = row.get(f"{rc}_sharpe", "—")
            print(f"  {row['regime']:<18} {row['n_pct']:>4.1f}%  {str(mean):>13}  {str(sharpe):>8}")
        ic_val = r["gmm"]["ic"].get(rc, float("nan"))
        print(f"  {'IC(' + rc + ')':<18} {'':>5}  {'':>13}  {'':>8}  |  {ic_val:>+.4f}")
        hr = r["gmm"]["hit"].get(rc, {})
        print(
            f"  {'Hit rate':<18} {'':>5}  hits={hr.get('hits', '?')}/{hr.get('total directional', '?')}  {hr.get('hit_rate', '—')}%"
        )

    # Save CSV
    os.makedirs(args.out_dir, exist_ok=True)
    rows_csv = []
    for r in results:
        if "error" in r:
            continue
        for classifier in ["rule", "gmm"]:
            stats = r[classifier]["stats"]
            ic = r[classifier]["ic"]
            hit = r[classifier]["hit"]
            for _, row in stats.iterrows():
                rec = {
                    "asset": r["asset"],
                    "classifier": classifier,
                    "regime": row["regime"],
                    "n": row["n"],
                    "n_pct": row["n_pct"],
                }
                for col in stats.columns:
                    if col not in ("regime", "n", "n_pct"):
                        rec[col] = row[col]
                for k, v in ic.items():
                    rec[f"ic_{k}"] = round(v, 4) if not np.isnan(v) else None
                for k, v in hit.items():
                    rec[f"hit_{k}"] = v
                rows_csv.append(rec)
    df_csv = pd.DataFrame(rows_csv)
    out_path = os.path.join(args.out_dir, "regime_comparison.csv")
    df_csv.to_csv(out_path, index=False)
    print(f"\n\nSaved to {out_path}")


if __name__ == "__main__":
    main()
