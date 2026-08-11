"""
Exit-path contribution profile (quant audit 2026-08-07, item 3).

The audit's first exit report: for each asset, how much PnL comes from each
exit path, in money AND in net R, per asset x regime. This is what exposes the
payoff-geometry problem (e.g. "WR 72-73% with PF ~1.06 = most wins end at
TP1/BE while rare full SLs eat the profit").

Paths (classified from the Trade audit fields):

    SL_pre_TP1      stop hit before TP1                       (full 3-step loss)
    BE_early        early-breakeven scratch before TP1        (BE trigger < 1.0)
    TP1_BE          TP1 hit, then scratched at breakeven
    TP1_SL          TP1 hit, then stopped (stop moved to entry => scratch)
    TP1_timeout     TP1 hit, horizon expired before TP2
    TP2_exit        TP1+TP2 hit, exit before TP3 (BE/stop/timeout)
    TP2_trailing    TP1+TP2 hit, trailing-stop exit
    TP3             TP1+TP2+TP3 runner (20% remainder)
    trailing        trailing exit after TP1+TP2

net R = trade pnl / money(|entry - initial_stop|)  (initial stop BEFORE any
BE/trailing move; money = price distance x volume x point_value_lot).

Also reports the payoff geometry: average winner/loser in R and the breakeven
win rate BE_WR = |avg_loss| / (avg_win + |avg_loss|), which is what WR must
exceed for the grid to pay.

Honesty: same walk-forward as run_backtest (per-fold XGBoost, temp-file
models, no look-ahead). Synthetic fallback (biased probs) for tests/no DB.
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.deflated_sharpe import (
    _make_synthetic_wf_df,
    _inject_biased_probs,
    _build_fold_frames,
    _SYNTH_DEFAULTS,
)
from scripts.run_backtest import merge_asset_cfg
from model.ensemble_backtest import EnsembleBacktester


def classify_path(exit_reason: str, tp1_hit: bool, tp2_hit: bool) -> str:
    """Map (exit_reason, tp1_hit, tp2_hit) to an exit-path bucket."""
    if exit_reason == "tp3_runner":
        return "TP3"
    if exit_reason == "trailing":
        return "TP2_trailing" if tp2_hit else "trailing_early"
    if exit_reason == "timeout":
        if tp2_hit:
            return "TP2_exit"
        if tp1_hit:
            return "TP1_timeout"
        return "timeout_pre_tp1"
    if exit_reason == "breakeven":
        if tp2_hit:
            return "TP2_exit"
        if tp1_hit:
            return "TP1_BE"
        return "BE_early"
    if exit_reason == "stop":
        if tp1_hit:
            return "TP1_SL"
        return "SL_pre_TP1"
    return f"other:{exit_reason}"


def build_exit_profile(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                       max_folds: int | None = None) -> dict:
    """Run the honest walk-forward for one asset and profile exit paths.

    Returns a dict with per-trade rows (fold, regime, path, net_r, pnl) and
    aggregate tables (overall + per regime).
    """
    windows, frames = _build_fold_frames(df_full, cfg, asset_key, max_folds)
    if not windows:
        raise ValueError(
            f"No walk-forward folds produced for {asset_key} "
            f"({len(df_full)} rows).")

    from backtest.metrics import trades_to_dataframe

    rows = []
    for fold_i, fdf in enumerate(frames):
        cfg_run = merge_asset_cfg(cfg, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
        trades = engine.run(fdf.reset_index(drop=True))
        # N6 (audit 2026-08-10): derive per-trade fields from the SAME
        # trades_to_dataframe used by compute_r_metrics, so the per-trade net_r
        # and the aggregate R numbers below cannot drift apart (a fourth
        # independent copy of the R formula previously lived here).
        tdf = trades_to_dataframe(trades)
        for _, row in tdf.iterrows():
            init_stop = row["initial_stop_price"]
            risk_dist = abs(row["entry_price"] - init_stop) if init_stop else 0.0
            vol = row["volume"] if pd.notna(row["volume"]) else engine.volume
            risk_money = risk_dist * vol * engine.point_value_lot
            net_r = float(row["pnl"] / risk_money) if risk_money > 0 else float("nan")
            rows.append({
                "fold": fold_i,
                "regime": row["regime_at_entry"],
                "direction": row["direction"],
                "exit_reason": row["exit_reason"],
                "path": classify_path(row["exit_reason"], row.get("tp1_hit", False),
                                      row.get("tp2_hit", False)),
                "net_r": round(net_r, 4),
                "pnl": round(float(row["pnl"]), 4),
            })

    trades_df = pd.DataFrame(rows)
    overall = _aggregate(trades_df)
    per_regime = {}
    if len(trades_df):
        for reg, g in trades_df.groupby("regime"):
            per_regime[str(reg)] = _aggregate(g)
    return {"asset": asset_key, "n_folds": len(windows), "n_trades": len(rows),
            "trades": trades_df, "overall": overall, "per_regime": per_regime}


def _aggregate(tdf: pd.DataFrame) -> dict:
    """Per-path PnL contribution + payoff geometry for a trade frame."""
    if len(tdf) == 0:
        return {"n": 0, "paths": {}, "payoff": {}}
    out = {"n": int(len(tdf))}
    paths = {}
    for path, g in tdf.groupby("path"):
        paths[path] = {
            "n": int(len(g)),
            "share_pct": round(100.0 * len(g) / len(tdf), 1),
            "total_pnl": round(float(g["pnl"].sum()), 2),
            "pnl_share_pct": round(100.0 * g["pnl"].sum() / max(tdf["pnl"].sum(), 1e-9), 1),
            "mean_net_r": round(float(g["net_r"].mean()), 3),
            "win_rate_pct": round(100.0 * float((g["pnl"] > 0).mean()), 1),
        }
    out["paths"] = paths
    wins = tdf[tdf["pnl"] > 0]["net_r"]
    losses = tdf[tdf["pnl"] <= 0]["net_r"]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    be_wr = 100.0 * avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else float("nan")
    out["payoff"] = {
        "avg_win_R": round(avg_win, 3),
        "avg_loss_R": round(avg_loss, 3),  # positive magnitude of the mean loss
        "breakeven_wr_pct": round(be_wr, 1),
        "actual_wr_pct": round(100.0 * float((tdf["pnl"] > 0).mean()), 1),
        "net_expectancy_R": round(float(tdf["net_r"].mean()), 3),
    }
    return out


def print_report(prof: dict) -> None:
    a = prof["asset"]
    print(f"\n=== Exit-path profile: {a} ({prof['n_folds']} folds, {prof['n_trades']} trades) ===")
    o = prof["overall"]
    pay = o.get("payoff", {})
    if o.get("n", 0) == 0:
        print("  no trades on this data slice.")
        return
    print(f"Payoff geometry: avg win {pay.get('avg_win_R')} R | avg loss "
          f"{pay.get('avg_loss_R')} R | breakeven WR needed "
          f"{pay.get('breakeven_wr_pct')}% | actual WR {pay.get('actual_wr_pct')}% | "
          f"net expectancy {pay.get('net_expectancy_R')} R/trade")
    hdr = (f"{'path':<15}{'n':>6}{'share%':>7}{'PnL$':>9}{'PnL%':>7}"
           f"{'meanR':>8}{'WR%':>7}")
    print(hdr)
    print("-" * len(hdr))
    for path in sorted(o["paths"], key=lambda p: -abs(o["paths"][p]["total_pnl"])):
        pth = o["paths"][path]
        print(f"{path:<15}{pth['n']:>6}{pth['share_pct']:>7.1f}{pth['total_pnl']:>9.1f}"
              f"{pth['pnl_share_pct']:>7.1f}{pth['mean_net_r']:>8.3f}{pth['win_rate_pct']:>7.1f}")
    print("\nPer regime (paths sorted by |PnL contribution|):")
    for reg, agg in prof["per_regime"].items():
        if agg.get("n", 0) == 0:
            continue
        top = sorted(agg["paths"].items(), key=lambda kv: -abs(kv[1]["total_pnl"]))[:4]
        desc = ", ".join(f"{p}: {v['total_pnl']:.1f}$ ({v['mean_net_r']:.2f}R, n={v['n']})"
                         for p, v in top)
        print(f"  {reg:<14} n={agg['n']:<5} {desc}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Exit-path contribution profile per asset.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out", default=None, help="Per-trade CSV (default: logs/exit_profile_<asset>.csv)")
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
        from scripts.run_backtest import load_asset_history, build_full_df
        raw = load_asset_history(db_path, timeframe, args.asset)
        df = build_full_df(cfg, raw, db_path=db_path, asset_key=args.asset)
        print(f"[exit-profile] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(f"[exit-profile] WARNING: cannot load real data ({exc.__class__.__name__}); "
              "SYNTHETIC demo — results are NOT real.")
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)

    prof = build_exit_profile(cfg, args.asset, df, max_folds=args.max_folds)
    prof["synthetic"] = synthetic
    print_report(prof)

    os.makedirs("logs", exist_ok=True)
    out_csv = args.out or f"logs/exit_profile_{args.asset.lower()}.csv"
    prof["trades"].to_csv(out_csv, index=False)
    # Summary sidecar (JSON-serializable aggregates)
    import json
    summary = {k: v for k, v in prof.items() if k != "trades"}
    with open(out_csv.replace(".csv", ".json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"[exit-profile] per-trade CSV -> {out_csv}")


if __name__ == "__main__":
    main()
