"""How calibration + rule/ML blending suppress the second direction - all 5 assets.

For each production model this measures, on a recent real-data window:

  1. RAW probabilities from the underlying XGBoost (pre-calibration)
  2. CALIBRATED probabilities through the Platt sigmoid (as deployed)
  3. Direction balance (% bars p_long > 0.5) BEFORE vs AFTER calibration
  4. Edge gate effect (min_edge) - how many signals survive, in which direction
  5. Rule vote vs ML vote agreement - what the blend does to the minority side

The point is to show WHERE the second direction dies: in the sigmoid transfer,
in the edge gate, or in the rule+ML blend.

Usage:
    python -m scripts.diag_calib_blend_direction [--bars N] [--assets XAUUSD,BTCUSD]
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

from config.loader import load_config, resolve_asset_timeframe
from scripts.run_backtest import load_asset_history
from scripts.train_mt5 import build_full_df  # production builder (bifurcation features)

ASSET_TF = {
    "XAUUSD": "M15",
    "XAGUSD": "M15",
    "BTCUSD": "M5",
    "EURUSD": "H1",
    "GBPUSD": "H1",
}


def _tf_for(cfg: dict, asset: str) -> str:
    return resolve_asset_timeframe(cfg, asset)


def sigmoid_params(model) -> tuple[float, float] | None:
    """Return (a, b) of the Platt sigmoid, or None if no calibration wrapper."""
    if not hasattr(model, "calibrated_classifiers_"):
        return None
    cc = model.calibrated_classifiers_[0]
    cal = getattr(cc, "calibrators", [None])[0]
    if cal is None:
        return None
    a = getattr(cal, "a_", None)
    b = getattr(cal, "b_", None)
    if a is None or b is None:
        return None
    return float(a), float(b)


def raw_and_calibrated(model, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Raw XGBoost P(long) (col 1) and calibrated P(long)."""
    if hasattr(model, "calibrated_classifiers_"):
        cc = model.calibrated_classifiers_[0]
        raw = cc.estimator.predict_proba(X)[:, 1]
    else:
        raw = model.predict_proba(X)[:, 1]
    cal = model.predict_proba(X)[:, 1]
    return raw, cal


def _summary(arr: np.ndarray) -> dict:
    return {
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr)),
        "p5": float(np.percentile(arr, 5)),
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "share_gt_0.5": float((arr > 0.5).mean()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=30000,
                    help="recent bars to score per asset (0 = all)")
    ap.add_argument("--assets", default=None, help="comma list, default all 5")
    args = ap.parse_args()

    cfg = load_config()
    db = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    ens_cfg = cfg.get("ensemble", {})
    min_edge = float(ens_cfg.get("min_edge", 0.15))
    min_ml_prob = float(ens_cfg.get("min_ml_probability", 0.55))
    ml_floor = float(ens_cfg.get("ml_confidence_floor", 0.62))
    w_rule = float(ens_cfg.get("rule_weight", 0.20))
    w_ml = float(ens_cfg.get("ml_weight", 0.80))

    assets = [a.strip() for a in (args.assets or "").split(",") if a.strip()] or list(ASSET_TF)
    print("=" * 104)
    print("CALIBRATION + BLEND DIRECTION SUPPRESSION - all assets")
    print(f"min_edge={min_edge} min_ml_prob={min_ml_prob} ml_floor={ml_floor} "
          f"rule_weight={w_rule} ml_weight={w_ml}")
    print("=" * 104)

    rows = []
    for asset in assets:
        tf = _tf_for(cfg, asset)
        mp = cfg["assets"].get(asset, {}).get("model_path")
        if not mp or not os.path.exists(mp):
            print(f"\n[{asset}] no model at {mp}")
            continue
        import joblib
        bundle = joblib.load(mp)
        model = bundle["model"]
        cols = bundle["feature_cols"]
        ab = sigmoid_params(model)

        raw_df = load_asset_history(db, tf, asset)
        if args.bars and len(raw_df) > args.bars:
            raw_df = raw_df.tail(args.bars).reset_index(drop=True)
        df = build_full_df(raw_df, cfg, db_path=db, asset_key=asset, timeframe=tf)
        df = df.reset_index(drop=True)

        # Synthesize missing regime_* one-hot columns from the regime column
        # (ModelPredictor does this at inference; we do the same for the raw
        # estimator path).
        if "regime" in df.columns:
            from regime.classifier import regime_onehot_df
            onehot = regime_onehot_df(df).reindex(columns=cols)
            for c in cols:
                if c.startswith("regime_") and c not in df.columns and c in onehot.columns:
                    df[c] = onehot[c].astype(float)

        X = df[cols].astype(float).values
        raw_p, cal_p = raw_and_calibrated(model, X)

        s_raw = _summary(raw_p)
        s_cal = _summary(cal_p)

        # Exact transfer check: cal == 1/(1+exp(a*raw+b))?
        transfer_note = "n/a (no calibration)"
        if ab:
            a, b = ab
            pred = 1.0 / (1.0 + np.exp(a * raw_p + b))
            max_diff = float(np.abs(cal_p - pred).max())
            corr = float(np.corrcoef(raw_p, cal_p)[0, 1])
            transfer_note = f"max|cal - 1/(1+exp(a*raw+b))|={max_diff:.2e}, corr(raw,cal)={corr:.4f}"

        # direction balance before/after calibration
        raw_dir_long = raw_p > 0.5
        cal_dir_long = cal_p > 0.5
        # edge-gate survival on calibrated probs
        cal_edge = np.abs(2 * cal_p - 1.0)
        cal_pass = cal_edge >= min_edge
        cal_pass_long = cal_p > 0.5
        # min_ml_prob gate
        cal_pmax = np.maximum(cal_p, 1 - cal_p)
        prob_pass = cal_pmax >= min_ml_prob

        # rule vote (from regime at that bar) - need regime column
        regime_col = df["regime"] if "regime" in df.columns else None
        if regime_col is not None:
            regs = regime_col.map(lambda v: v.value if hasattr(v, "value") else str(v))
            rule_vote = np.where(regs == "trend_up", 1, np.where(regs == "trend_down", -1, 0))
        else:
            rule_vote = np.zeros(len(df), dtype=int)
        ml_vote = np.where(cal_p > 0.5, 1, -1)
        agree = (rule_vote == ml_vote) | (rule_vote == 0) | (ml_vote == 0)

        print(f"\n{'#' * 100}")
        print(f"# {asset}  TF={tf}  bars={len(df)}  model={os.path.basename(mp)}")
        if ab:
            a, b = ab
            crossover = -b / a if a != 0 else float("nan")
            # sklearn _SigmoidCalibration: cal = 1/(1+exp(a*T+b)) -> d(cal)/d(T)
            # = -a*exp(a*T+b)/(1+exp(a*T+b))^2, so the SIGN of the slope is -a.
            if abs(a) < 1e-6:
                slope = "FLAT (degenerate constant)"
            else:
                slope = "INCREASING" if a < 0 else "DECREASING"
            print(f"# Platt sigmoid: a={a:.4f}  b={b:.4f}  crossover(raw p_long)={crossover:.4f}  "
                  f"slope={slope}  (cal = 1/(1+exp(a*raw+b)))")
        else:
            print("# NO calibration wrapper - probabilities are RAW")
        print(f"{'#' * 100}")
        print("\n-- P(long) distribution: RAW (XGBoost) vs CALIBRATED --")
        print(f"  {'':16s} {'mean':>7s} {'std':>7s} {'p5':>7s} {'p50':>7s} {'p95':>7s} {'%>0.5':>7s}")
        print(f"  {'raw':16s} {s_raw['mean']:7.4f} {s_raw['std']:7.4f} {s_raw['p5']:7.4f} "
              f"{s_raw['p50']:7.4f} {s_raw['p95']:7.4f} {100*s_raw['share_gt_0.5']:6.1f}%")
        print(f"  {'calibrated':16s} {s_cal['mean']:7.4f} {s_cal['std']:7.4f} {s_cal['p5']:7.4f} "
              f"{s_cal['p50']:7.4f} {s_cal['p95']:7.4f} {100*s_cal['share_gt_0.5']:6.1f}%")

        print("\n-- Transfer check --")
        print(f"  {transfer_note}")

        print("\n-- Direction balance (long share of ALL bars) --")
        print(f"  raw says LONG:      {100*raw_dir_long.mean():6.1f}%   ({raw_dir_long.sum()}/{len(df)})")
        print(f"  cal says LONG:      {100*cal_dir_long.mean():6.1f}%   ({cal_dir_long.sum()}/{len(df)})")
        print(f"  direction flipped by cal: {(raw_dir_long != cal_dir_long).mean()*100:.1f}% of bars")

        print("\n-- Gate funnel (on CALIBRATED probs) --")
        n0 = len(df)
        n1 = int(prob_pass.sum())          # min_ml_probability
        n2 = int(cal_pass.sum())           # min_edge
        n2_long = int((cal_pass & cal_pass_long).sum())
        n2_short = int((cal_pass & ~cal_pass_long).sum())
        print(f"  all bars:                    {n0:>8d}")
        print(f"  pass min_ml_prob ({min_ml_prob}):    {n1:>8d} ({100*n1/n0:5.1f}%)")
        print(f"  pass min_edge ({min_edge}):        {n2:>8d} ({100*n2/n0:5.1f}%)  "
              f"-> LONG {n2_long} / SHORT {n2_short}")
        if n2:
            print(f"  long share of edge-pass:     {100*n2_long/n2:5.1f}%")

        print("\n-- Rule + ML blend (on CALIBRATED probs) --")
        print(f"  rule_vote from regime: +1(up) {int((rule_vote==1).sum())} / -1(down) "
              f"{int((rule_vote==-1).sum())} / 0 {int((rule_vote==0).sum())}")
        print(f"  ml_vote long share: {100*(ml_vote==1).mean():5.1f}%")
        print(f"  agree (incl rule=0/ml=0): {100*agree.mean():5.1f}%")
        # how often rule opposes ml on edge-pass signals
        ep = cal_pass
        if ep.sum():
            opp = (rule_vote != 0) & (ml_vote != 0) & (rule_vote != ml_vote) & ep
            print(f"  hard opposition on edge-pass: {int(opp.sum())} bars "
                  f"({100*opp.sum()/ep.sum():4.1f}% of edge-pass)")

        rows.append({
            "asset": asset, "tf": tf, "bars": len(df),
            "sigmoid_a": ab[0] if ab else None, "sigmoid_b": ab[1] if ab else None,
            "raw_std": s_raw["std"], "cal_std": s_cal["std"],
            "raw_share_long": s_raw["share_gt_0.5"], "cal_share_long": s_cal["share_gt_0.5"],
            "edge_pass_n": n2, "edge_pass_long_n": n2_long,
            "long_share_edge_pass": 100*n2_long/n2 if n2 else float("nan"),
        })

    if rows:
        print("\n" + "=" * 104)
        print("SUMMARY")
        print("=" * 104)
        print(pd.DataFrame(rows).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
