"""
Exit-geometry calibration from MFE/MAE (quant audit 2026-08-07, Claude plan
action 4) — the alternative to grid-search that does NOT burn trials.

Recipe (from the audit):
  1. For each signal (ignoring exits) measure over the horizon H:
     MFE = max favorable excursion in steps, MAE = max adverse excursion.
  2. Slice by asset x regime. Compute P(MFE >= 1/2/3/5) and MAE quantiles.
  3. Calibrate:
       SL  = the tightest MAE level with P(MAE >= SL | MFE >= 2) <= 0.20
             (the stop must not knock out trades that would have reached TP2)
       TP1 = q55-q60 of MFE,  TP2 = q75-q80 of MFE
       trailing vs fixed TP3: trail if P(MFE >= 5 | trend, MFE >= 2) >= 0.25,
             fixed TP3 if < 0.15, else optional/neutral.

Honesty: barriers are calibrated on the TRAIN part of every fold ONLY and
never evaluated on the test window here — this script only MEASURES and
proposes. Applying the policy is a separate, pre-registered step (compare the
proposed regime_overrides vs the current grid with the same walk-forward;
see scripts/deflated_sharpe.py and the commented config block).

Usage:
    python -m scripts.exit_calibration --asset GBPUSD
    python -m scripts.exit_calibration --asset EURUSD --max-folds 10
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtest.walk_forward import generate_windows
from config.loader import load_config
from scripts.deflated_sharpe import (
    _SYNTH_DEFAULTS,
    _inject_biased_probs,
    _make_synthetic_wf_df,
)
from scripts.diag_r_metrics import _mfe_mae, _signal_mask
from scripts.run_backtest import merge_asset_cfg


def calibrate_stop(mfe_mae_df: pd.DataFrame, max_p: float = 0.20) -> dict:
    """Tightest SL (in steps) with P(MAE >= SL | MFE >= 2) <= max_p.

    Evaluated over the MAE quantile ladder; returns the chosen SL, the
    achieved conditional probability and the sample sizes. NaN when the
    MFE>=2 subsample is too small.
    """
    d = mfe_mae_df.dropna(subset=["mfe", "mae"])
    reached = d[d["mfe"] >= 2.0]
    if len(reached) < 10:
        return {"sl_steps": None, "p_mae_ge_sl_given_mfe2": None, "n_mfe2": len(reached)}
    ladder = np.unique(np.quantile(d["mae"], [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95]))
    best_sl, best_p = None, None
    for sl in ladder:
        p = float((reached["mae"] >= sl).mean())
        if p <= max_p:
            best_sl, best_p = float(sl), p
    return {"sl_steps": best_sl, "p_mae_ge_sl_given_mfe2": best_p, "n_mfe2": int(len(reached))}


def calibrate_targets(mfe: np.ndarray, tp1_q: float = 0.55, tp2_q: float = 0.75) -> dict:
    """TP1/TP2 from MFE quantiles (audit: TP1 = q55-q60, TP2 = q75-q80)."""
    m = np.asarray(mfe, dtype=float)
    m = m[np.isfinite(m)]
    if len(m) < 10:
        return {"tp1_steps": None, "tp2_steps": None, "n": len(m)}
    return {"tp1_steps": float(np.quantile(m, tp1_q)),
            "tp2_steps": float(np.quantile(m, tp2_q)),
            "n": int(len(m))}


def trailing_decision(mfe_mae_df: pd.DataFrame, trend_regimes=("trend_up", "trend_down")) -> dict:
    """P(MFE >= 5 | trend regime AND MFE >= 2): >= 0.25 -> trailing runner
    recommended; < 0.15 -> fixed TP3; in between -> neutral."""
    d = mfe_mae_df.dropna(subset=["mfe"])
    if "regime" in d.columns:
        d = d[d["regime"].isin(trend_regimes)]
    reached = d[d["mfe"] >= 2.0]
    if len(reached) < 10:
        return {"p_mfe5_given_trend_mfe2": None, "verdict": "insufficient data", "n": len(reached)}
    p = float((reached["mfe"] >= 5.0).mean())
    verdict = ("trailing_recommended" if p >= 0.25 else
               "fixed_tp3" if p < 0.15 else "neutral")
    return {"p_mfe5_given_trend_mfe2": round(p, 3), "verdict": verdict, "n": int(len(reached))}


def calibrate_per_regime(mfe_mae_df: pd.DataFrame) -> dict:
    """Full audit recipe per regime."""
    out = {}
    for reg, g in mfe_mae_df.groupby("regime"):
        out[str(reg)] = {
            **calibrate_stop(g),
            **calibrate_targets(g["mfe"].to_numpy(dtype=float)),
        }
    out["_trailing"] = trailing_decision(mfe_mae_df)
    return out


def run_calibration(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                    max_folds: int | None = None) -> dict:
    """Calibrate on the TRAIN part of each fold only (never the test window)."""
    wf_cfg = cfg["backtest"]["walk_forward"]
    lab_cfg = merge_asset_cfg(cfg, asset_key, "labeling")["labeling"]
    horizon = int(lab_cfg.get("horizon_candles_n", 36))
    windows = generate_windows(df_full, wf_cfg["train_window_days"],
                               wf_cfg["test_window_days"], wf_cfg["step_days"])
    if not windows:
        raise ValueError(f"No walk-forward folds produced for {asset_key}.")
    if max_folds is not None:
        windows = windows[:max_folds]

    frames = []
    for w in windows:
        train = df_full[(df_full["timestamp_utc"] >= w.train_start_ts) &
                        (df_full["timestamp_utc"] < w.train_end_ts)]
        frames.append(train)

    all_mm = []
    for tr in frames:
        mm = _mfe_mae(tr, horizon)
        sig = _signal_mask(tr)
        regs = tr["regime"].to_numpy() if "regime" in tr.columns else np.full(len(tr), "range")
        for i in np.where(sig)[0]:
            reg = regs[i]
            if hasattr(reg, "value"):
                reg = reg.value
            all_mm.append({"regime": str(reg),
                           "mfe": float(mm["mfe"].iloc[i]) if np.isfinite(mm["mfe"].iloc[i]) else np.nan,
                           "mae": float(mm["mae"].iloc[i]) if np.isfinite(mm["mae"].iloc[i]) else np.nan})
    mdf = pd.DataFrame(all_mm)
    return {"asset": asset_key, "n_folds": len(frames), "n_signals": len(mdf),
            "per_regime": calibrate_per_regime(mdf.dropna(subset=["mfe"]) if len(mdf) else mdf)}


def print_report(d: dict) -> None:
    print(f"\n=== Exit-geometry calibration (train-only): {d['asset']} ===")
    print(f"Folds (train windows): {d['n_folds']} | signals: {d['n_signals']}")
    for reg, v in d["per_regime"].items():
        if reg == "_trailing":
            t = v
            print(f"Trailing: P(MFE>=5 | trend, MFE>=2) = {t['p_mfe5_given_trend_mfe2']} "
                  f"-> {t['verdict']} (n={t['n']})")
            continue
        print(f"  {reg:<14} SL={v['sl_steps']} (P(MAE>=SL|MFE>=2)={v['p_mae_ge_sl_given_mfe2']}) "
              f"| TP1={v['tp1_steps']} TP2={v['tp2_steps']} (n={v['n']})")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Train-only exit-geometry calibration from MFE/MAE.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out", default=None, help="JSON output (default: logs/exit_calibration_<asset>.json)")
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
        print(f"[calib] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(f"[calib] WARNING: cannot load real data ({exc.__class__.__name__}); "
              "SYNTHETIC demo — results are NOT real.")
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)

    d = run_calibration(cfg, args.asset, df, max_folds=args.max_folds)
    d["synthetic"] = synthetic
    print_report(d)

    os.makedirs("logs", exist_ok=True)
    out_json = args.out or f"logs/exit_calibration_{args.asset.lower()}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"[calib] -> {out_json}")


if __name__ == "__main__":
    main()
