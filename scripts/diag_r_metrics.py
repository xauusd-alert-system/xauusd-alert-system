"""
Week-1 measurement tool (quant audit 2026-08-07, Claude 5 Opus plan §0-§1).

One comprehensive diagnostics run per asset, NO new features, NO grid:

- R-multiplicator metrics: E[R], sigma[R], skew/kurtosis of R, payoff
  geometry (avg win/loss in R, breakeven WR) and the exit-path bucket table
  (stop / BE / TP1-only / TP2 / TP3) with count, share, mean R and R
  contribution. R = trade pnl / money(|entry - initial_stop|).
- cost_ratio = (spread + 2*slippage + commission_in_price) / mean_step
  (audit norm: < 8-10%, red zone > 15%) + stress at 1.5x / 2.0x costs.
- events-per-feature = trades / len(FEATURE_COLUMNS) (audit rule of thumb:
  >= 10 events per feature; H1 assets with ~120 events / 46 features are
  over-parameterized).
- MFE/MAE in steps over the labeling horizon for ALL signal rows, per
  regime: P(MFE >= 1/2/3/5), MAE quantiles (p50/p80/p90). This is the
  input to exit-geometry calibration (audit Action 4) -- no barrier is
  tuned here, only measured.
- Fold sign test + positive-fold share of VALID folds.

Honesty: same walk-forward as run_backtest (per-fold XGBoost, temp-file
models, no look-ahead). Synthetic fallback for tests / no DB.

Usage:
    python -m scripts.diag_r_metrics --asset GBPUSD
    python -m scripts.diag_r_metrics --asset XAUUSD --timeframe M15   # TF comparison
"""

import argparse
import json
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
from scripts.exit_profile import classify_path
from scripts.run_backtest import merge_asset_cfg
from backtest.metrics import (
    compute_r_metrics,
    block_bootstrap_t,
    fold_sign_test,
    summarize_folds,
)
from model.ensemble_backtest import EnsembleBacktester
from model.trainer import FEATURE_COLUMNS


def _mfe_mae(df: pd.DataFrame, horizon: int, atr_col: str = "atr") -> pd.DataFrame:
    """Vectorized MFE/MAE in steps over the NEXT `horizon` bars (exclusive of
    the signal bar itself): MFE = (max future high - close)/atr,
    MAE = (close - min future low)/atr. Reversed-rolling trick: a rolling
    window on the reversed series is a FORWARD-looking window."""
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    atr = df[atr_col].to_numpy(dtype=float) if atr_col in df.columns else np.full(len(df), np.nan)

    n = len(df)
    h_rev = high[::-1]
    l_rev = low[::-1]
    # rolling window of size horizon on the reversed array (min_periods=1 so
    # the tail of the series still gets a partial window)
    fmax_incl = pd.Series(h_rev).rolling(horizon, min_periods=1).max().to_numpy()[::-1]
    fmin_incl = pd.Series(l_rev).rolling(horizon, min_periods=1).min().to_numpy()[::-1]
    # shift(-1): exclude the signal bar itself (entry at its close)
    fmax = np.full(n, np.nan)
    fmin = np.full(n, np.nan)
    fmax[:-1] = fmax_incl[1:]
    fmin[:-1] = fmin_incl[1:]

    with np.errstate(divide="ignore", invalid="ignore"):
        mfe = (fmax - close) / atr
        mae = (close - fmin) / atr
    mfe[~np.isfinite(mfe)] = np.nan
    mae[~np.isfinite(mae)] = np.nan
    return pd.DataFrame({"mfe": mfe, "mae": mae}, index=df.index)


def _signal_mask(df: pd.DataFrame, cfg: dict = None, asset_key: str = None) -> np.ndarray:
    """Rows where the ensemble would actually open a trade (same gates as
    EnsembleBacktester): regime not suppressed, session allowed, ml prob above the
    per-asset minimum. N5 (audit 2026-08-10): this previously hard-coded 0.55 and
    {compression, reversal_watch}, so the MFE/MAE table was computed on a signal
    set that did NOT match the tradable set (it included the suppressed `range`
    regime and everything below the real per-asset bar). Now it reads the same
    per-asset ensemble config the live trader uses."""
    from regime.classifier import RegimeLabel
    mask = np.zeros(len(df), dtype=bool)
    p_longs = df.get("ml_p_long", pd.Series(0.5, index=df.index)).to_numpy(dtype=float)
    p_shorts = df.get("ml_p_short", pd.Series(0.5, index=df.index)).to_numpy(dtype=float)
    regimes = df["regime"].to_numpy() if "regime" in df.columns else np.full(len(df), "range")

    if cfg and asset_key:
        merged = merge_asset_cfg(cfg, asset_key, "ensemble")
        ens = merged.get("ensemble", {})
        min_conf = float(ens.get("min_confidence_to_alert", 0.55))
        suppressed = set(ens.get("suppress_regimes", ["compression", "reversal_watch", "range"]))
    else:
        min_conf = 0.55
        suppressed = {"compression", "reversal_watch", "no_trade", "range"}
    suppressed.add("no_trade")

    for i in range(len(df)):
        reg = regimes[i]
        if isinstance(reg, RegimeLabel):
            reg = reg.value
        if str(reg) in suppressed:
            continue
        if max(p_longs[i], p_shorts[i]) < min_conf:
            continue
        mask[i] = True
    return mask


def run_diagnostics(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                    max_folds: int | None = None) -> dict:
    """Full Week-1 measurement for one asset (honest walk-forward)."""
    windows, frames = _build_fold_frames(df_full, cfg, asset_key, max_folds)
    if not windows:
        raise ValueError(f"No walk-forward folds produced for {asset_key}.")

    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    bt_cfg = cfg.get("backtest", {})
    volume = float(bt_cfg.get("volume", 0.01))
    point_value_lot = float(asset_cfg.get("point_value_lot", bt_cfg.get("point_value_lot", 100.0)))
    spread = float(asset_cfg.get("spread_usd", bt_cfg.get("spread_points", 25) / 100.0))
    slippage = float(asset_cfg.get("slippage_usd", bt_cfg.get("slippage_points", 5) / 100.0))
    commission = float(bt_cfg.get("commission_per_trade", 0.07))
    commission_price = commission / (volume * point_value_lot)

    horizon = int(merge_asset_cfg(cfg, asset_key, "labeling")["labeling"].get(
        "horizon_candles_n", 36))

    fold_results = []
    all_trades = []
    mfe_mae_rows = []
    rejected_rows = []
    for fold_i, fdf in enumerate(frames):
        cfg_run = merge_asset_cfg(cfg, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
        fdf_run = fdf.reset_index(drop=True)
        trades = engine.run(fdf_run)
        tdf = trades_to_df(trades)
        if len(tdf):
            all_trades.append(tdf)
            fold_results.append({"fold": fold_i, "n_trades": len(tdf),
                                 "total_pnl": float(tdf["pnl"].sum()),
                                 "profit_factor": _pf(tdf["pnl"].to_numpy())})
        # Queue loss (audit week 1): signals rejected because a position was
        # already open — simulate their hypothetical honest entry.
        for rej in engine.rejected_signals:
            t_sim = engine.simulate_blocked_entry(fdf_run, rej["bar"], rej["direction"])
            if t_sim is None:
                continue
            risk = abs(t_sim.entry_price - t_sim.initial_stop_price) * volume * point_value_lot
            rejected_rows.append({
                "fold": fold_i,
                "regime": rej["regime"],
                "direction": rej["direction"],
                "pnl": float(t_sim.pnl),
                "r": float(t_sim.pnl / risk) if risk > 1e-12 else float("nan"),
            })
        # MFE/MAE for all signal rows in this fold
        mm = _mfe_mae(fdf, horizon)
        sig = _signal_mask(fdf, cfg=cfg, asset_key=asset_key)
        regs = fdf["regime"].to_numpy() if "regime" in fdf.columns else np.full(len(fdf), "range")
        for i in np.where(sig)[0]:
            reg = regs[i]
            if hasattr(reg, "value"):
                reg = reg.value
            mfe_mae_rows.append({"fold": fold_i, "regime": str(reg),
                                 "mfe": float(mm["mfe"].iloc[i]) if np.isfinite(mm["mfe"].iloc[i]) else np.nan,
                                 "mae": float(mm["mae"].iloc[i]) if np.isfinite(mm["mae"].iloc[i]) else np.nan})

    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    r_metrics = compute_r_metrics(trades_df, point_value_lot=point_value_lot, volume=volume)

    # cost ratio on the mean realized step (TP1 distance) and stress
    steps = None
    if len(trades_df):
        steps = (trades_df["tp1_price"] - trades_df["entry_price"]).abs().to_numpy() \
            if "tp1_price" in trades_df.columns else None
    mean_step = float(np.nanmean(steps)) if steps is not None and len(steps) else float("nan")
    total_cost = spread + 2.0 * slippage + commission_price
    cost_ratio = total_cost / mean_step if np.isfinite(mean_step) and mean_step > 0 else float("nan")

    # events per feature
    n_events = int(trades_df.shape[0]) if len(trades_df) else 0
    events_per_feature = n_events / len(FEATURE_COLUMNS) if FEATURE_COLUMNS else 0.0

    # MFE/MAE per regime
    mfe_mae = {}
    if mfe_mae_rows:
        mmdf = pd.DataFrame(mfe_mae_rows)
        for reg, g in mmdf.groupby("regime"):
            mfe = g["mfe"].dropna()
            mae = g["mae"].dropna()
            if len(mfe) == 0:
                continue
            mfe_mae[str(reg)] = {
                "n_signals": int(len(g)),
                "p_mfe_ge_1": round(float((mfe >= 1).mean()), 3),
                "p_mfe_ge_2": round(float((mfe >= 2).mean()), 3),
                "p_mfe_ge_3": round(float((mfe >= 3).mean()), 3),
                "p_mfe_ge_5": round(float((mfe >= 5).mean()), 3),
                "mae_p50": round(float(mae.quantile(0.50)), 3),
                "mae_p80": round(float(mae.quantile(0.80)), 3),
                "mae_p90": round(float(mae.quantile(0.90)), 3),
            }

    # Queue-loss summary: E[R] of rejected signals vs E[R] of taken trades.
    queue_loss = {"n_rejected": 0, "n_simulated": 0, "mean_r_rejected": None,
                  "mean_r_taken": None, "verdict": None}
    if rejected_rows:
        rdf = pd.DataFrame(rejected_rows)
        r_rej = rdf["r"].dropna().to_numpy(dtype=float)
        r_taken = None
        if len(all_trades):
            tdf_all = pd.concat(all_trades, ignore_index=True)
            risk_all = (tdf_all["entry_price"] - tdf_all["initial_stop_price"]).abs() * volume * point_value_lot
            ok = risk_all > 1e-12
            r_taken = (tdf_all.loc[ok, "pnl"] / risk_all[ok]).to_numpy(dtype=float)
        queue_loss = {
            "n_rejected": len(rejected_rows),
            "n_simulated": int(len(r_rej)),
            "mean_r_rejected": round(float(r_rej.mean()), 4) if len(r_rej) else None,
            "mean_r_taken": round(float(r_taken.mean()), 4) if r_taken is not None and len(r_taken) else None,
        }
        if queue_loss["mean_r_rejected"] is not None and queue_loss["mean_r_taken"] is not None:
            queue_loss["verdict"] = (
                "queue constraint destroys edge: rejected E[R] >= taken E[R]"
                if queue_loss["mean_r_rejected"] >= queue_loss["mean_r_taken"]
                else "queue constraint costs little: rejected E[R] < taken E[R]")

    # fold sign test + consistency
    fold_results = [r for r in fold_results if r["n_trades"] > 0]
    pos_folds = int(sum(1 for r in fold_results if r["total_pnl"] > 0))
    n_valid = len(fold_results)
    sign = fold_sign_test(pos_folds, n_valid) if n_valid else {}
    summary = summarize_folds([{**r, "n_trades": r["n_trades"]} for r in fold_results])

    r_series = None
    if len(trades_df):
        risk = (trades_df["entry_price"] - trades_df["initial_stop_price"]).abs() * volume * point_value_lot
        ok = risk > 1e-12
        r_series = (trades_df.loc[ok, "pnl"] / risk[ok]).to_numpy(dtype=float)

    return {
        "asset": asset_key,
        "n_folds": len(windows),
        "n_valid_folds": n_valid,
        "n_trades": n_events,
        "n_features": len(FEATURE_COLUMNS),
        "events_per_feature": round(events_per_feature, 1),
        "r_metrics": r_metrics,
        "t_block": round(block_bootstrap_t(r_series), 3) if r_series is not None and len(r_series) >= 2 else None,
        "cost_ratio_pct": round(100.0 * cost_ratio, 1) if np.isfinite(cost_ratio) else None,
        "cost_total_price": round(total_cost, 6),
        "mean_step": round(mean_step, 6) if np.isfinite(mean_step) else None,
        "cost_stress": {
            "note": "cost stress x1.5/x2.0 runs via scripts.deflated_sharpe (cost_stress) "
                    "and scripts.run_backtest with adjusted spread/slippage",
        },
        "fold_sign_test": sign,
        "fold_summary": summary,
        "queue_loss": queue_loss,
        "mfe_mae": mfe_mae,
        "trades": trades_df,
    }


def trades_to_df(trades):
    from backtest.metrics import trades_to_dataframe
    return trades_to_dataframe(trades)


def _pf(pnls: np.ndarray) -> float:
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    gp, gl = float(wins.sum()), float(-losses.sum())
    return (gp / gl) if gl > 0 else 999.0


def print_report(d: dict) -> None:
    a = d["asset"]
    # O1/§4.6 (audit 2026-08-10): the fail-open synthetic fallback must be loud.
    # Previously the `synthetic` flag only landed in the JSON sidecar, so the
    # printed report and the CSV looked like real measurements.
    if d.get("synthetic"):
        print("\n!!! ⚠️ SYNTHETIC DEMO DATA — RESULTS ARE NOT REAL !!!")
        print("    Real DB was not available; a biased synthetic signal was used.\n")
    print(f"\n=== Week-1 diagnostics: {a} ===")
    print(f"Folds: {d['n_folds']} (valid {d['n_valid_folds']}) | trades: {d['n_trades']} | "
          f"features: {d['n_features']} | events/feature: {d['events_per_feature']} "
          f"(audit rule: >= 10)")
    r = d["r_metrics"]
    if r.get("n", 0) == 0:
        print("  no trades on this data slice.")
        return
    print(f"R metrics: E[R]={r['mean_r']} | sigma[R]={r['std_r']} | skew={r['skew_r']} | "
          f"kurt_ex={r['kurtosis_excess_r']} | t_block={d['t_block']}")
    print(f"Payoff geometry: avg win {r['avg_win_r']}R | avg loss {r['avg_loss_r']}R | "
          f"breakeven WR {r['breakeven_wr_pct']}% | actual WR {r['actual_wr_pct']}%")
    if d.get("cost_ratio_pct") is not None:
        zone = "OK" if d["cost_ratio_pct"] <= 10 else ("RED" if d["cost_ratio_pct"] > 15 else "borderline")
        print(f"Costs: total {d['cost_total_price']} price units | mean step {d['mean_step']} | "
              f"cost_ratio = {d['cost_ratio_pct']}% ({zone}; norm <8-10%, red >15%)")
    print(f"Buckets (R contribution):")
    for b, v in sorted(r["buckets"].items(), key=lambda kv: -abs(kv[1]["r_contribution_pct"])):
        print(f"  {b:<16} n={v['n']:<6} share={v['share_pct']:>5.1f}%  meanR={v['mean_r']:>7.3f}  "
              f"R-contr={v['r_contribution_pct']:>7.1f}%")
    if d["fold_sign_test"].get("n_folds"):
        st = d["fold_sign_test"]
        print(f"Fold sign test: {st['n_positive']}/{st['n_folds']} positive valid folds "
              f"(z={st['z']}, p={st['p_one_sided']})")
    ql = d["queue_loss"]
    if ql["n_rejected"]:
        print(f"Queue loss: {ql['n_rejected']} rejected signals (simulated "
              f"{ql['n_simulated']}) | E[R] rejected = {ql['mean_r_rejected']} vs "
              f"E[R] taken = {ql['mean_r_taken']} -> {ql['verdict']}")
    if d["mfe_mae"]:
        print("MFE/MAE (steps over horizon, per regime; exit calibration input):")
        for reg, v in d["mfe_mae"].items():
            print(f"  {reg:<14} n={v['n_signals']:<6} P(MFE>=1)={v['p_mfe_ge_1']:.2f} "
                  f"P(MFE>=2)={v['p_mfe_ge_2']:.2f} P(MFE>=3)={v['p_mfe_ge_3']:.2f} "
                  f"P(MFE>=5)={v['p_mfe_ge_5']:.2f} | MAE p50/p80/p90 = "
                  f"{v['mae_p50']}/{v['mae_p80']}/{v['mae_p90']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Week-1 R-metrics / cost / MFE-MAE diagnostics.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--timeframe", default=None, help="Override timeframe (TF comparison)")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--max-folds", type=int, default=None)
    parser.add_argument("--out", default=None, help="Per-trade CSV (default: logs/diag_r_metrics_<asset>.csv)")
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
        print(f"[diag] Real data: {len(df)} {timeframe} rows from {db_path}")
    except Exception as exc:
        synthetic = True
        print(f"[diag] WARNING: cannot load real data ({exc.__class__.__name__}); "
              "SYNTHETIC demo — results are NOT real.")
        spec = _SYNTH_DEFAULTS.get(args.asset, dict(price=1.28, atr=0.0014, freq="1h"))
        freq = spec["freq"]
        bars_per_day = {"5min": 288, "15min": 96, "1h": 24, "4h": 6}.get(freq, 24)
        n = min(bars_per_day * 1500, 150_000)
        df = _make_synthetic_wf_df(n, spec["price"], spec["atr"], freq)
        df = _inject_biased_probs(df)

    d = run_diagnostics(cfg, args.asset, df, max_folds=args.max_folds)
    d["synthetic"] = synthetic
    print_report(d)

    os.makedirs("logs", exist_ok=True)
    out_csv = args.out or f"logs/diag_r_metrics_{args.asset.lower()}.csv"
    out_df = d["trades"].copy()
    if synthetic:
        # O1/§4.6: flag the artifact itself so a saved CSV cannot be mistaken for
        # a real measurement on a fresh clone (no DB present -> synthetic fallback).
        out_df["synthetic"] = synthetic
    out_df.to_csv(out_csv, index=False)
    summary = {k: v for k, v in d.items() if k != "trades"}
    with open(out_csv.replace(".csv", ".json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"[diag] per-trade CSV -> {out_csv}" + (" (SYNTHETIC)" if synthetic else ""))


if __name__ == "__main__":
    main()
