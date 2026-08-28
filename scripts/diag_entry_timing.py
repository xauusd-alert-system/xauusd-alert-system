"""
Look-ahead check at entry (quant audit 2026-08-07, Claude plan week 1):

    "Сигнал считается по закрытию бара -> вход обязан быть по открытию
     следующего. Тест: прогони «вход по close сигнального бара» vs
     «вход по open следующего». Если эдж существенно проседает во втором
     варианте — часть твоего результата это заглядывание в будущее."

Runs the honest walk-forward twice per asset:
  - next_open   (honest):  entry at the OPEN of the bar after the signal bar
  - signal_close (look-ahead measurement): entry at the CLOSE of the signal bar

Compares E[R], PF, t_block, WR and reports the mean gap between the signal
bar's close and the next bar's open (in ATR) — the size of the advantage the
look-ahead fill would get.

Usage:
    python -m scripts.diag_entry_timing --asset GBPUSD
    python -m scripts.diag_entry_timing --asset XAUUSD --max-folds 10
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtest.metrics import (
    block_bootstrap_t,
    compute_r_metrics,
    trades_to_dataframe,
)
from config.loader import load_config
from model.ensemble_backtest import EnsembleBacktester
from scripts.deflated_sharpe import (
    _SYNTH_DEFAULTS,
    _build_fold_frames,
    _inject_biased_probs,
    _make_synthetic_wf_df,
)
from scripts.run_backtest import merge_asset_cfg


def run_fill_modes(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                   max_folds: int | None = None) -> dict:
    """Run the honest walk-forward under both fill modes; returns per-mode
    trade frames + aggregate R metrics + the close->next-open gap stats."""
    windows, frames = _build_fold_frames(df_full, cfg, asset_key, max_folds)
    if not windows:
        raise ValueError(f"No walk-forward folds produced for {asset_key}.")

    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    bt_cfg = cfg.get("backtest", {})
    volume = float(bt_cfg.get("volume", 0.01))
    point_value_lot = float(asset_cfg.get("point_value_lot", bt_cfg.get("point_value_lot", 100.0)))

    modes = {"next_open": [], "signal_close": []}
    gaps = []  # |close[signal] - open[signal+1]| / atr[signal+1]
    for fdf in frames:
        cfg_run = merge_asset_cfg(cfg, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        for mode in modes:
            engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
            engine.fill_mode = mode
            trades = engine.run(fdf.reset_index(drop=True))
            modes[mode].append(trades_to_dataframe(trades))
        # Gap stats over the whole frame (every bar, not only signals — the
        # structural cost of the honest fill)
        c = fdf["close"].to_numpy(dtype=float)
        o = fdf["open"].to_numpy(dtype=float)
        a = fdf["atr"].to_numpy(dtype=float) if "atr" in fdf.columns else np.full(len(fdf), np.nan)
        valid = np.isfinite(a) & (a > 1e-12)
        if valid[:-1].any():
            gaps.append(np.abs(c[:-1][valid[:-1]] - o[1:][valid[:-1]]) / a[1:][valid[:-1]])

    out = {}
    for mode, frames_list in modes.items():
        tdf = pd.concat(frames_list, ignore_index=True) if frames_list else pd.DataFrame()
        r = compute_r_metrics(tdf, point_value_lot=point_value_lot, volume=volume)
        r_series = None
        if len(tdf):
            risk = (tdf["entry_price"] - tdf["initial_stop_price"]).abs() * volume * point_value_lot
            ok = risk > 1e-12
            r_series = (tdf.loc[ok, "pnl"] / risk[ok]).to_numpy(dtype=float)
        out[mode] = {
            "n_trades": int(len(tdf)),
            "mean_r": r["mean_r"],
            "std_r": r["std_r"],
            "pf": round(float(np.nansum(tdf["pnl"].clip(lower=0)) /
                              max(-np.nansum(tdf["pnl"].clip(upper=0)), 1e-12)), 3)
            if len(tdf) else 0.0,
            "win_rate_pct": r["actual_wr_pct"],
            "t_block": block_bootstrap_t(r_series) if r_series is not None and len(r_series) >= 2 else None,
        }
    all_gaps = np.concatenate(gaps) if gaps else np.array([])
    out["gap_stats"] = {
        "n": int(len(all_gaps)),
        "mean_atr": round(float(all_gaps.mean()), 4) if len(all_gaps) else None,
        "p50_atr": round(float(np.median(all_gaps)), 4) if len(all_gaps) else None,
        "p90_atr": round(float(np.quantile(all_gaps, 0.90)), 4) if len(all_gaps) else None,
    }
    out["asset"] = asset_key
    return out


def print_report(d: dict) -> None:
    a = d["asset"]
    print(f"\n=== Entry-timing look-ahead check: {a} ===")
    h = d["gap_stats"]
    if h["n"]:
        print(f"Close[signal] -> Open[next] gap: mean {h['mean_atr']} ATR | "
              f"p50 {h['p50_atr']} | p90 {h['p90_atr']} (the size of the "
              "look-ahead advantage)")
    for mode in ("next_open", "signal_close"):
        m = d[mode]
        label = "HONEST (next open)" if mode == "next_open" else "LOOK-AHEAD (signal close)"
        print(f"{label:<28} n={m['n_trades']:<6} E[R]={m['mean_r']:>7.3f} "
              f"PF={m['pf']:>6.2f} WR={m['win_rate_pct']:>5.1f}% t_block={m['t_block']}")
    no, sc = d["next_open"], d["signal_close"]
    if no["n_trades"] and sc["n_trades"]:
        degrade = (no["mean_r"] - sc["mean_r"]) / max(abs(sc["mean_r"]), 1e-9)
        verdict = ("WARNING: the honest fill eats most of the edge — part of the "
                   "result is look-ahead" if degrade < -0.3 else
                   "OK: honest fill retains the edge (degradation < 30%)")
        print(f"Relative E[R] change honest-vs-lookahead: {degrade:+.0%}  -> {verdict}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Look-ahead check at entry (fill modes).")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out", default=None, help="JSON output (default: logs/diag_entry_timing_<asset>.json)")
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
        print(f"[timing] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(f"[timing] WARNING: cannot load real data ({exc.__class__.__name__}); "
              "SYNTHETIC demo — results are NOT real.")
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)

    d = run_fill_modes(cfg, args.asset, df, max_folds=args.max_folds)
    d["synthetic"] = synthetic
    print_report(d)

    os.makedirs("logs", exist_ok=True)
    out_json = args.out or f"logs/diag_entry_timing_{args.asset.lower()}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"[timing] -> {out_json}")


if __name__ == "__main__":
    main()
