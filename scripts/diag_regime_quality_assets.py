"""Per-asset regime-quality diagnostic.

Answers: why is the ADX/DI regime label anti-predictive (trend regimes trade
worse than range)? Compares per-regime trade outcomes and the classifier's
forward-predictive power across all 5 assets.

Reads per-trade CSVs produced by the diag_*_prepost walk-forward runs
(logs/dir_prepost_<asset>_postfix.csv, exit_profile_xauusd.csv for XAUUSD).

Usage:
    python -m scripts.diag_regime_quality_assets [--csv path ...]
"""

from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

ASSET_FILES = {
    "XAUUSD": "logs/exit_profile_xauusd.csv",
    "BTCUSD": "logs/dir_prepost_btcusd_m5_postfix.csv",
    "EURUSD": "logs/dir_prepost_eurusd_postfix.csv",
    "GBPUSD": "logs/dir_prepost_gbpusd_postfix.csv",
    "XAGUSD": "logs/dir_prepost_xagusd_postfix.csv",
}


def load(asset: str, override: str | None) -> pd.DataFrame:
    path = override or ASSET_FILES[asset]
    df = pd.read_csv(path)
    # Normalize R column: exit_profile uses net_r, dir_prepost uses R
    if "net_r" in df.columns and "R" not in df.columns:
        df["R"] = df["net_r"]
    # exit_profile has `path` (SL_pre_TP1/BE_early/TP3/timeout...) instead of exit_reason
    if "exit_reason" not in df.columns and "path" in df.columns:
        df["exit_reason"] = df["path"]
    df["regime"] = df["regime"].astype(str).str.strip().str.lower()
    df["direction"] = df["direction"].astype(str).str.strip().str.lower()
    return df


def per_regime(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for reg, g in df.groupby("regime"):
        stops = g["exit_reason"].astype(str).str.contains("SL|STOP|stop", na=False).sum()
        tp3 = g["exit_reason"].astype(str).str.contains("TP3|TP2", na=False).sum()
        rows.append(
            {
                "regime": reg,
                "n": len(g),
                "share_pct": 100 * len(g) / len(df),
                "meanR": g["R"].mean(),
                "sumR": g["R"].sum(),
                "wr_pct": 100 * (g["R"] > 0).mean(),
                "stops_pct": 100 * stops / len(g),
                "tp2_3_pct": 100 * tp3 / len(g),
            }
        )
    out = pd.DataFrame(rows).sort_values("n", ascending=False)
    return out


def regime_baseline_compare(df: pd.DataFrame) -> dict:
    """Compare each regime vs the unconditional baseline (all trades)."""
    base_mean = df["R"].mean()
    base_wr = (df["R"] > 0).mean()
    out = {}
    for reg, g in df.groupby("regime"):
        n = len(g)
        if n < 5:
            continue
        se = g["R"].std() / np.sqrt(n)
        t = (g["R"].mean() - base_mean) / se if se > 0 else 0.0
        out[reg] = {
            "n": n,
            "meanR": g["R"].mean(),
            "base_meanR": base_mean,
            "delta_R": g["R"].mean() - base_mean,
            "t_vs_base": t,
            "wr": (g["R"] > 0).mean(),
            "base_wr": base_wr,
        }
    return out


def direction_balance(df: pd.DataFrame) -> dict:
    """Per-regime direction mix (long/short split)."""
    out = {}
    for reg, g in df.groupby("regime"):
        if len(g) == 0:
            continue
        longs = g["direction"].isin(["long", "buy", "l"]).sum()
        shorts = g["direction"].isin(["short", "sell", "s"]).sum()
        out[reg] = {"n": len(g), "long": int(longs), "short": int(shorts)}
    return out


def regime_predictive_quality(df: pd.DataFrame) -> dict:
    """Classifier quality: can the regime label predict the trade sign better
    than a coin flip? Uses aligned-trend longs (trend_up+long, trend_down+short)
    vs opposed/range."""
    out = {}
    for reg, g in df.groupby("regime"):
        if len(g) < 5:
            continue
        long = g["direction"].isin(["long", "buy", "l"])
        short = g["direction"].isin(["short", "sell", "s"])
        if reg == "trend_up":
            aligned = long
            opposed = short
        elif reg == "trend_down":
            aligned = short
            opposed = long
        else:
            # range/compression: no directional claim
            continue
        a = g.loc[aligned, "R"]
        o = g.loc[opposed, "R"]
        out[reg] = {
            "aligned_n": len(a),
            "aligned_meanR": a.mean() if len(a) else np.nan,
            "opposed_n": len(o),
            "opposed_meanR": o.mean() if len(o) else np.nan,
            "aligned_wr": 100 * (a > 0).mean() if len(a) else np.nan,
            "opposed_wr": 100 * (o > 0).mean() if len(o) else np.nan,
            "edge_aligned_minus_opposed": (a.mean() if len(a) else np.nan) - (o.mean() if len(o) else np.nan),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--csv", action="append", default=None, help="override CSV per asset, format ASSET=PATH (repeatable)"
    )
    args = ap.parse_args()

    overrides = {}
    for kv in args.csv or []:
        a, _, p = kv.partition("=")
        overrides[a.strip().upper()] = p.strip()

    print("=" * 100)
    print("REGIME QUALITY ACROSS ASSETS (walk-forward per-trade CSVs)")
    print("=" * 100)

    summary_rows = []
    for asset, default_path in ASSET_FILES.items():
        if not os.path.exists(overrides.get(asset, default_path)):
            print(f"\n[{asset}] no CSV at {overrides.get(asset, default_path)} - skipped")
            continue
        df = load(asset, overrides.get(asset))
        print(f"\n{'#' * 90}")
        print(f"# {asset}  ({len(df)} trades)")
        print(f"{'#' * 90}")
        print("\n-- Per-regime outcomes --")
        print(per_regime(df).to_string(index=False))
        print("\n-- Direction mix per regime (long/short) --")
        print(pd.DataFrame(direction_balance(df)).T.to_string())
        print("\n-- Regime vs baseline (t-test vs unconditional meanR) --")
        bl = regime_baseline_compare(df)
        if bl:
            print(pd.DataFrame(bl).T.round(3).to_string())
        print("\n-- Trend-alignment edge (aligned vs opposed R) --")
        pq = regime_predictive_quality(df)
        if pq:
            print(pd.DataFrame(pq).T.round(3).to_string())

        # overall rank: which regime is best/worst for this asset
        pr = per_regime(df)
        if len(pr) >= 2:
            best = pr.iloc[0]["regime"] if pr.iloc[0]["meanR"] >= pr.iloc[1]["meanR"] else pr.iloc[1]["regime"]
            worst = pr.iloc[-1]["regime"] if pr.iloc[-1]["meanR"] <= pr.iloc[-2]["meanR"] else pr.iloc[-2]["regime"]
            summary_rows.append(
                {
                    "asset": asset,
                    "n": len(df),
                    "totalR": df["R"].sum(),
                    "best_regime": best,
                    "best_meanR": pr.loc[pr["meanR"].idxmax(), "meanR"],
                    "worst_regime": pr.loc[pr["meanR"].idxmin(), "regime"],
                    "worst_meanR": pr["meanR"].min(),
                }
            )

    if summary_rows:
        print("\n" + "=" * 100)
        print("SUMMARY: best/worst regime by meanR per asset")
        print("=" * 100)
        print(pd.DataFrame(summary_rows).to_string(index=False))


if __name__ == "__main__":
    main()
