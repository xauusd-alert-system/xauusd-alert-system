"""
Time-stop analysis (quant audit 2026-08-07, Claude plan question 4e):

    "Построй кривую E[ΔR | удержано k баров]. Там, где она выходит на
     плато или разворачивается, поставь time-exit. У M5-систем это обычно
     20-60 баров."

For each trade we know (entry_ts, exit_ts, net R, exit_reason). Conditional
analysis: E[final net R | position still open after h bars] — i.e. among
trades held at least h bars, their final net R. If this conditional
expectancy turns <= 0 at some h, holding beyond h is (historically)
value-destroying; a time-stop there is justified. Also reports the share of
trades still open at h (duration survival) so the stop's cost is visible.

Implementation: honest walk-forward (same engine), per-trade bars-held from
the bar index difference; conditional curve + bootstrap CI (percentile).

Usage:
    python -m scripts.diag_time_stop --asset GBPUSD
    python -m scripts.diag_time_stop --asset XAUUSD --max-folds 10
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


def run_time_stop(
    cfg: dict, asset_key: str, df_full: pd.DataFrame, max_folds: int | None = None, max_h: int | None = None
) -> dict:
    windows, frames = _build_fold_frames(df_full, cfg, asset_key, max_folds)
    if not windows:
        raise ValueError(f"No walk-forward folds produced for {asset_key}.")

    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    bt_cfg = cfg.get("backtest", {})
    volume = float(bt_cfg.get("volume", 0.01))
    point_value_lot = float(asset_cfg.get("point_value_lot", bt_cfg.get("point_value_lot", 100.0)))
    horizon = int(merge_asset_cfg(cfg, asset_key, "labeling")["labeling"].get("horizon_candles_n", 36))
    max_h = max_h or horizon

    rows = []
    for fdf in frames:
        cfg_run = merge_asset_cfg(cfg, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
        fdf_run = fdf.reset_index(drop=True)
        ts = fdf_run["timestamp_utc"].to_numpy(dtype=np.int64)
        for t in engine.run(fdf_run):
            risk = (
                abs(t.entry_price - t.initial_stop_price) * t.volume * point_value_lot if t.initial_stop_price else 0.0
            )
            net_r = float(t.pnl / risk) if risk > 1e-12 else float("nan")
            i0 = int(np.where(ts == t.entry_ts)[0][0]) if np.any(ts == t.entry_ts) else -1
            i1 = int(np.where(ts == t.exit_ts)[0][0]) if np.any(ts == t.exit_ts) else -1
            held = (i1 - i0) if (i0 >= 0 and i1 >= 0) else int((t.exit_ts - t.entry_ts) / max(86400, 1))
            rows.append({"held": held, "net_r": net_r, "exit_reason": t.exit_reason})

    if len(rows) < 10:
        return {"asset": asset_key, "n_trades": len(rows), "verdict": "insufficient trades", "curve": []}

    rdf = pd.DataFrame(rows)
    curve = []
    rng = np.random.default_rng(0)
    for h in range(1, min(max_h, int(rdf["held"].max())) + 1):
        sub = rdf[rdf["held"] >= h]
        if len(sub) < 10:
            continue
        r = sub["net_r"].to_numpy(dtype=float)
        boot = np.empty(500)
        for b in range(500):
            boot[b] = rng.choice(r, size=len(r), replace=True).mean()
        curve.append(
            {
                "h_bars": int(h),
                "n_still_open": int(len(sub)),
                "survival_pct": round(100.0 * len(sub) / len(rdf), 1),
                "e_net_r_given_held_ge_h": round(float(r.mean()), 4),
                "ci95_lo": round(float(np.percentile(boot, 2.5)), 4),
                "ci95_hi": round(float(np.percentile(boot, 97.5)), 4),
                "e_net_r_tp1_not_reached": round(float(r[sub["exit_reason"] == "timeout"].mean()), 4)
                if (sub["exit_reason"] == "timeout").any()
                else None,
            }
        )

    # first h where the point estimate of conditional expectancy <= 0
    time_stop = None
    for c in curve:
        if c["e_net_r_given_held_ge_h"] <= 0.0:
            time_stop = c["h_bars"]
            break
    verdict = (
        f"time-stop at ~{time_stop} bars"
        if time_stop
        else "no h with negative conditional expectancy; time-stop unlikely to help"
    )
    return {
        "asset": asset_key,
        "n_trades": len(rows),
        "horizon": horizon,
        "time_stop_h": time_stop,
        "verdict": verdict,
        "curve": curve,
    }


def print_report(d: dict) -> None:
    print(f"\n=== Time-stop analysis: {d['asset']} ===")
    print(f"Trades: {d['n_trades']} | horizon: {d['horizon']}")
    print("E[net R | held >= h] (survival % | point estimate | 95% CI):")
    for c in d["curve"][:: max(1, len(d["curve"]) // 12)]:
        print(
            f"  h={c['h_bars']:>3}  surv={c['survival_pct']:>5.1f}%  "
            f"E[R]={c['e_net_r_given_held_ge_h']:+.3f}  "
            f"CI=[{c['ci95_lo']:+.3f}, {c['ci95_hi']:+.3f}]"
        )
    print(f"Verdict: {d['verdict']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Conditional time-stop analysis.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out", default=None, help="JSON output (default: logs/time_stop_<asset>.json)")
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
        print(f"[timestop] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(
            f"[timestop] WARNING: cannot load real data ({exc.__class__.__name__}); "
            "SYNTHETIC demo — results are NOT real."
        )
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)

    d = run_time_stop(cfg, args.asset, df, max_folds=args.max_folds)
    d["synthetic"] = synthetic
    print_report(d)

    os.makedirs("logs", exist_ok=True)
    out_json = args.out or f"logs/time_stop_{args.asset.lower()}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, default=str)
    print(f"[timestop] -> {out_json}")


if __name__ == "__main__":
    main()
