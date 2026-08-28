"""
Meta-labeling pre-check (quant audit 2026-08-07, Claude plan question 2).

The audit's one-day pre-check BEFORE any meta-model work:

    "Возьми уже существующую вероятность первичной модели и посчитай
     purged-OOS AUC против метки «TP2 раньше SL», по активам.
     Если AUC < 0.53 — meta-labeling на текущем информационном наборе не
     сработает. Если AUC >= 0.55 — сайзинг даст Sharpe +10..+25% и
     просадку -15..-30%."

Implementation: honest walk-forward; for every executed trade we take the
primary model's probability at the signal bar and the outcome
"reached TP2 before a stop" (tp2_hit and pnl >= 0). OOF probabilities are
pooled across folds -> ROC AUC (sklearn), plus a decile table of mean net R
per primary-probability decile (monotonicity gate for sizing) and Brier/ECE
(simple binning) as the calibration check.

Verdict:
    AUC < 0.53  -> meta-sizing not supported by current information
    AUC 0.53-0.55 -> borderline, revisit after feature/TF work
    AUC >= 0.55 -> sizing may add value; proceed with the audit's gates
                   (Brier skill > 0, calibration slope 0.8-1.2, monotone
                   decile net-R, uplift in >= 60% outer folds, DSR not worse)

Usage:
    python -m scripts.diag_meta_precheck --asset GBPUSD
    python -m scripts.diag_meta_precheck --asset EURUSD --max-folds 10
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from model.ensemble_backtest import EnsembleBacktester
from scripts.deflated_sharpe import (
    _SYNTH_DEFAULTS,
    _build_fold_frames,
    _inject_biased_probs,
    _make_synthetic_wf_df,
)
from scripts.run_backtest import merge_asset_cfg


def run_meta_precheck(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                      max_folds: int | None = None) -> dict:
    """Per-trade OOF primary-prob vs "TP2 before SL" + AUC/deciles/Brier."""
    windows, frames = _build_fold_frames(df_full, cfg, asset_key, max_folds)
    if not windows:
        raise ValueError(f"No walk-forward folds produced for {asset_key}.")

    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    bt_cfg = cfg.get("backtest", {})
    volume = float(bt_cfg.get("volume", 0.01))
    point_value_lot = float(asset_cfg.get("point_value_lot", bt_cfg.get("point_value_lot", 100.0)))

    rows = []
    for fdf in frames:
        cfg_run = merge_asset_cfg(cfg, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
        fdf_run = fdf.reset_index(drop=True)
        trades = engine.run(fdf_run)
        for t in trades:
            # probability at the signal bar = one bar before the entry bar
            # (entry happened at bar i, signal at bar i-1); we stored entry_ts,
            # so locate the entry row and take the previous row's p.
            idx_entry = np.where(fdf_run["timestamp_utc"].values == t.entry_ts)[0]
            if len(idx_entry) == 0:
                continue
            i = int(idx_entry[0])
            if i < 1:
                continue
            p_long = float(fdf_run["ml_p_long"].iloc[i - 1])
            p_short = float(fdf_run["ml_p_short"].iloc[i - 1])
            p = p_long if t.direction == 1 else p_short
            reached_tp2 = bool(t.tp2_hit and t.pnl is not None and t.pnl >= 0)
            risk = abs(t.entry_price - t.initial_stop_price) * t.volume * point_value_lot \
                if t.initial_stop_price else 0.0
            rows.append({
                "p": float(p),
                "y_tp2_before_sl": int(reached_tp2),
                "net_r": float(t.pnl / risk) if risk > 1e-12 else float("nan"),
            })

    if len(rows) < 30:
        return {"asset": asset_key, "n_trades": len(rows), "verdict": "insufficient trades",
                "auc": None, "brier": None, "ece": None, "deciles": []}

    rdf = pd.DataFrame(rows)
    from sklearn.metrics import brier_score_loss, roc_auc_score
    p = rdf["p"].to_numpy(dtype=float)
    y = rdf["y_tp2_before_sl"].to_numpy(dtype=int)
    try:
        auc = float(roc_auc_score(y, p))
    except ValueError:
        auc = None
    brier = float(brier_score_loss(y, p))
    base_rate = float(y.mean())
    brier_skill = max(0.0, base_rate * (1 - base_rate) - brier) / max(base_rate * (1 - base_rate), 1e-9)
    # ECE with 10 bins
    ece = 0.0
    if auc is not None:
        bins = np.linspace(0.0, 1.0, 11)
        for b in range(10):
            m = (p >= bins[b]) & (p < bins[b + 1])
            if m.sum() == 0:
                continue
            ece += (m.sum() / len(p)) * abs(float(p[m].mean()) - float(y[m].mean()))
    # decile net-R monotonicity (sizing gate)
    deciles = []
    try:
        rdf["decile"] = pd.qcut(p, 10, labels=False, duplicates="drop")
    except ValueError:
        rdf["decile"] = pd.cut(p, 10, labels=False)
    for d, g in rdf.groupby("decile"):
        deciles.append({"decile": int(d), "n": int(len(g)),
                        "mean_p": round(float(g["p"].mean()), 4),
                        "mean_net_r": round(float(g["net_r"].mean()), 4),
                        "frac_tp2": round(float(g["y_tp2_before_sl"].mean()), 3)})
    deciles.sort(key=lambda x: x["decile"])
    # monotonicity: Spearman of (mean_p, mean_net_r)
    mono = None
    if len(deciles) >= 4:
        from scipy.stats import spearmanr
        rho, _ = spearmanr([d["mean_p"] for d in deciles], [d["mean_net_r"] for d in deciles])
        mono = round(float(rho), 3)

    if auc is None:
        verdict = "insufficient outcome variation"
    elif auc < 0.53:
        verdict = "NOT supported: primary probabilities carry no TP2-before-SL information (AUC < 0.53)"
    elif auc < 0.55:
        verdict = "borderline (0.53-0.55): revisit after feature/TF work"
    else:
        verdict = "supported (AUC >= 0.55): meta-sizing may add value — proceed with the audit's gates"
    return {"asset": asset_key, "n_trades": len(rows), "auc": auc,
            "brier": round(brier, 4), "brier_skill": round(brier_skill, 4),
            "ece": round(ece, 4), "base_rate_tp2": round(base_rate, 4),
            "decile_monotonicity_spearman": mono, "deciles": deciles,
            "verdict": verdict}


def print_report(d: dict) -> None:
    print(f"\n=== Meta-labeling pre-check (TP2-before-SL): {d['asset']} ===")
    print(f"Trades: {d['n_trades']} | base rate TP2-before-SL: {d.get('base_rate_tp2')}")
    print(f"AUC = {d['auc']} | Brier = {d['brier']} (skill {d['brier_skill']}) | "
          f"ECE = {d['ece']} | decile monotonicity (Spearman) = {d.get('decile_monotonicity_spearman')}")
    print(f"Verdict: {d['verdict']}")
    if d.get("deciles"):
        print("Deciles (mean p | mean net R | frac TP2):")
        for dd in d["deciles"]:
            print(f"  d{dd['decile']:>2}: p={dd['mean_p']:.3f}  netR={dd['mean_net_r']:+.3f}  "
                  f"TP2frac={dd['frac_tp2']:.2f}  (n={dd['n']})")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Meta-labeling pre-check (AUC vs TP2-before-SL).")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out", default=None, help="JSON output (default: logs/meta_precheck_<asset>.json)")
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
        print(f"[meta] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(f"[meta] WARNING: cannot load real data ({exc.__class__.__name__}); "
              "SYNTHETIC demo — results are NOT real.")
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)

    d = run_meta_precheck(cfg, args.asset, df, max_folds=args.max_folds)
    d["synthetic"] = synthetic
    print_report(d)

    os.makedirs("logs", exist_ok=True)
    out_json = args.out or f"logs/meta_precheck_{args.asset.lower()}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"[meta] -> {out_json}")


if __name__ == "__main__":
    main()
