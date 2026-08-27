"""Fade-the-regime comparison on fresh walk-forward per-trade records.

For every asset the deployed model is one-directional (XAUUSD/XAGUSD long,
BTCUSD/EURUSD/GBPUSD short). The regime label was found anti-predictive
(trend_up longs lose, trend_down longs win). This script compares the model
AS-IS against a counterfactual FADE-THE-REGIME strategy on the SAME trade
universe of the walk-forward:

  d_fade = short if regime == trend_up
         = long  if regime == trend_down
         = model's choice otherwise (range)

  R_fade = R  if the model already traded the fade direction
         = -R otherwise (taking the opposite side realizes ~-R,
           ignoring spread/slippage asymmetry — a known approximation).

Scenario B flips only the with-regime trades inside the full universe;
Scenario C restricts to trend regimes only (strict fade, no range trades).

Also runs the shared SubsetScanner (Bonferroni/DSR) over direction x regime
cells on the ACTUAL R to show which cells are statistically real.

Usage:
    python -m scripts.diag_fade_regime
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.subset_scan import SubsetScanner  # noqa: E402

ASSETS = ["XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"]
LOGS = os.path.join(os.path.dirname(__file__), "..", "logs")


def _metrics(r: pd.Series) -> dict:
    r = r.dropna().astype(float)
    n = len(r)
    if n == 0:
        return {"n": 0, "sumR": 0.0, "WR%": float("nan"), "PF": float("nan")}
    wins = r[r > 0].sum()
    losses = abs(r[r < 0].sum())
    pf = wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0)
    return {
        "n": n,
        "sumR": round(float(r.sum()), 2),
        "WR%": round(100.0 * (r > 0).mean(), 1),
        "PF": round(float(pf), 2),
    }


def fade_r(row: pd.Series) -> float:
    """R realized by the fade-the-regime strategy on this trade."""
    regime = row["regime"]
    d = row["direction"]
    if regime == "trend_up":
        d_fade = "short"
    elif regime == "trend_down":
        d_fade = "long"
    else:
        d_fade = d  # range: keep the model's side
    r = float(row["R"])
    return r if d_fade == d else -r


def analyze(df: pd.DataFrame, asset: str) -> dict:
    out = {"asset": asset}
    out["model"] = _metrics(df["R"])
    out["fade_all"] = _metrics(df["R_fade"])

    trend_mask = df["regime"].isin(["trend_up", "trend_down"])
    out["model_trend"] = _metrics(df.loc[trend_mask, "R"])
    out["fade_trend"] = _metrics(df.loc[trend_mask, "R_fade"])

    # Trades where the model went WITH the regime (the ones fade flips).
    flip_mask = df["R_fade"] != df["R"]
    out["n_flipped"] = int(flip_mask.sum())
    out["model_on_flipped"] = _metrics(df.loc[flip_mask, "R"])
    out["fade_on_flipped"] = _metrics(df.loc[flip_mask, "R_fade"])
    return out


def main() -> None:
    rows = []
    for asset in ASSETS:
        path = os.path.join(LOGS, f"trade_quality_{asset.lower()}_dir.csv")
        if not os.path.isfile(path):
            print(f"SKIP {asset}: {path} not found")
            continue
        df = pd.read_csv(path)
        df["R_fade"] = df.apply(fade_r, axis=1)
        rows.append(analyze(df, asset))
        print(f"\n########## {asset} ##########")
        print(f"trades n={len(df)}  directions={df['direction'].value_counts().to_dict()}")
        print(f"regime     : {df['regime'].value_counts().to_dict()}")

        print("\n--- Model AS-IS vs FADE-THE-REGIME (same trade universe) ---")
        hdr = f"{'':18s} {'n':>5} {'sumR':>8} {'WR%':>6} {'PF':>5}"
        print(hdr)
        for label, key in [("model as-is (all)", "model"),
                           ("fade-the-regime (all)", "fade_all"),
                           ("model (trend regimes only)", "model_trend"),
                           ("fade (trend regimes only)", "fade_trend")]:
            m = rows[-1][key]
            print(f"{label:18s} {m['n']:>5} {m['sumR']:>+8.2f} {m['WR%']:>5.1f}% {m['PF']:>5.2f}")

        fl = rows[-1]
        print(f"\n--- On the {fl['n_flipped']} trades where model went WITH the regime "
              f"(fade flips the side) ---")
        print(f"  model keeps  : {fl['model_on_flipped']['sumR']:+.2f}R "
              f"(WR {fl['model_on_flipped']['WR%']}%)")
        print(f"  fade flips   : {fl['fade_on_flipped']['sumR']:+.2f}R "
              f"(WR {fl['fade_on_flipped']['WR%']}%)")

        # Direction x regime cells with multiple-testing correction.
        sc = SubsetScanner(df, r_col="R", min_trades=5)
        sc.add_groupby("direction").add_groupby("regime")
        res = sc.scan()
        print("\n--- Direction x regime cells (Bonferroni/DSR on ACTUAL R) ---")
        for r in res:
            if r.label in ("ALL", "direction=long", "direction=short",
                           "regime=trend_up", "regime=trend_down", "regime=range"):
                continue
            print(f"  {r.verdict:>8s} {r.label:40s} n={r.n:4d} "
                  f"sumR={r.sum_R:+7.2f} p_bonf={r.p_value_bonf:.3f} DSR={r.dsr:.2f}")

    print("\n\n========== CROSS-ASSET SUMMARY ==========")
    cols = ["asset", "model n", "model sumR", "model WR%", "model PF",
            "fade sumR", "fade WR%", "fade PF", "flip ΔR"]
    print(f"{'asset':8s} {'n':>5} {'model sumR':>10} {'model WR':>8} {'model PF':>7} "
          f"{'fade sumR':>9} {'fade WR':>7} {'fade PF':>7} {'flip gain':>9}")
    for r in rows:
        m, f, fl = r["model"], r["fade_all"], r["fade_on_flipped"]
        gain = f["sumR"] - m["sumR"]
        print(f"{r['asset']:8s} {m['n']:>5} {m['sumR']:>+10.2f} {m['WR%']:>7.1f}% "
              f"{m['PF']:>7.2f} {f['sumR']:>+9.2f} {f['WR%']:>6.1f}% {f['PF']:>7.2f} "
              f"{gain:>+9.2f}")


if __name__ == "__main__":
    main()
