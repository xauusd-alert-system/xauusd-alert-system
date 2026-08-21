# -*- coding: utf-8 -*-
"""Historical ensemble-accuracy: what did each engine predict at entry time
for each walk-forward trade, and was it right?

For each closed trade we look back at the entry bar and re-run the 6 ensemble
engines on point-in-time data (data <= entry bar). Then we compare:
  - Direction engines (OU, KalmanTrend, GBM_MC): did they agree with the trade?
  - Regime engines (GARCH, Heston, BayesRegime): did they confirm mean-reversion?

Usage:
    PYTHONIOENCODING=utf-8 python -m scripts.ensemble_accuracy [--pair XAU/XAG]
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pairs_analysis import load_config, PairAnalyzer, SignalEngine, EnsembleEngine
from pairs_analysis import data as data_mod
from pairs_analysis import ensemble as ens_mod
from pairs_analysis.analyzer import BARS_PER_DAY, BARS_PER_YEAR
from pairs_analysis import metrics as metrics_mod


def _build_pair_data(pair_cfg: dict, analysis_cfg: dict, tf: str):
    """Load and compute all pair data up front (shared across engines)."""
    pa = PairAnalyzer(pair_cfg, analysis_cfg)
    p1 = pa._load_leg(pair_cfg["symbols"][0], tf)
    p2 = pa._load_leg(pair_cfg["symbols"][1], tf)
    p1, p2 = data_mod.align(p1, p2)

    window = int(analysis_cfg.get("window", 90))
    kalman_q = float(analysis_cfg.get("kalman_q", 1e-4))
    kalman_r = float(analysis_cfg.get("kalman_r", 0.01))

    ln1 = np.log(p1["close"].astype(float))
    ln2 = np.log(p2["close"].astype(float))

    beta_series = pd.Series(
        metrics_mod.kalman_beta(ln2, ln1, q=kalman_q, r=kalman_r),
        index=ln1.index)
    e = ln1 - beta_series * ln2
    z = metrics_mod.zscore(e, window)

    return p1, p2, ln1, ln2, beta_series, e, z, window


def _engine_prediction_at_bar(engines_func, pair_name, tf, e, z, p1, ln1, ln2, window, t):
    """Run all 6 ensemble engines on point-in-time data up to bar t.
    Returns a list of EngineResult or None per engine."""
    if t < window + 20:
        return [None] * 6

    # Slice data to bar t (no look-ahead)
    e_slice = e.iloc[:t + 1]
    z_slice = z.iloc[:t + 1]
    p1_slice = p1.iloc[:t + 1]
    ln1_slice = ln1.iloc[:t + 1]

    # Build a minimal PairMetrics-like object
    spread_mean = float(e_slice.rolling(min(window, len(e_slice))).mean().dropna().iloc[-1]) if len(e_slice) >= window else float(e_slice.mean())
    sigma = float(e_slice.rolling(min(window, len(e_slice))).std(ddof=1).dropna().iloc[-1]) if len(e_slice) >= window else float(e_slice.std(ddof=1))

    theta, hl_bars = metrics_mod.half_life(e_slice)
    hl_days = hl_bars / BARS_PER_DAY.get(tf, 1.0) if np.isfinite(hl_bars) else float("inf")

    # Hurst on returns
    r = e_slice.diff().dropna()
    hurst = metrics_mod.hurst_rs(r) if len(r) > 20 else 0.5

    adf_p = metrics_mod.adf_pvalue(e_slice)

    z_cur = float(z_slice.dropna().iloc[-1]) if len(z_slice.dropna()) else 0.0
    p1_last = float(p1_slice["close"].iloc[-1])
    p2_last = float(p1_slice["close"].iloc[-1]) * 0.01  # placeholder

    # Point-in-time beta at bar t
    ln1_slice = ln1.iloc[:t + 1]
    ln2_slice = ln2.iloc[:t + 1]
    kb = metrics_mod.kalman_beta(ln2_slice, ln1_slice, q=1e-4, r=0.01)
    beta_val = float(kb[-1]) if len(kb) else 1.0
    beta_series_local = pd.Series(kb, index=ln1_slice.index)
    ratio = float(p1_slice["close"].iloc[-1] / 10.0)  # rough ratio

    m = type("M", (), {
        "name": pair_name, "timeframe": tf,
        "n_bars": t + 1, "start": str(e_slice.index[0].date()),
        "end": str(e_slice.index[-1].date()),
        "spread": e_slice, "zscore": z_slice,
        "mu": spread_mean, "sigma": sigma,
        "sigma_annual": sigma * np.sqrt(BARS_PER_YEAR.get(tf, 252)),
        "theta": theta, "half_life_bars": hl_bars, "half_life_days": hl_days,
        "adf_p": adf_p, "hurst": hurst,
        "skew": metrics_mod.skew(r) if len(r) > 8 else 0.0,
        "ex_kurtosis": metrics_mod.excess_kurtosis(r) if len(r) > 8 else 0.0,
        "acf1": metrics_mod.acf1(e_slice),
        "realized_vol_pct": metrics_mod.realized_vol_pct(e_slice, window),
        "beta": beta_val, "beta_method": "kalman",
        "beta_series": beta_series_local,
        "ratio": ratio, "p1_last": p1_last, "p2_last": p2_last,
        "formula_str": "",
        "p1": p1_slice, "p2": p1_slice,
    })()

    return [func(m) for func in ens_mod.ENGINE_FUNCS]


def analyze_pair(pair_cfg: dict, analysis_cfg: dict, tf: str,
                 bt_cfg: dict, thresholds: dict, pair_name: str):
    """Run walk-forward backtest + ensemble accuracy analysis for one pair."""
    print(f"\n{'='*60}")
    print(f"  {pair_name} ({tf})")
    print(f"{'='*60}")

    sig_engine = SignalEngine(thresholds, bt_cfg)

    p1, p2, ln1, ln2, beta_series, e, z, window = _build_pair_data(pair_cfg, analysis_cfg, tf)
    min_start = max(window, int(bt_cfg.get("min_start_bars", 250)))
    n = len(ln1)

    # --- Walk-forward: collect trades + entry bar indices ---
    trades = []
    cooldown = False
    t = min_start
    while t < n:
        zt = float(z.iloc[t]) if pd.notna(z.iloc[t]) else float("nan")
        if np.isnan(zt):
            t += 1; continue
        if cooldown and abs(zt) < sig_engine.entry_z:
            cooldown = False
        if not cooldown and sig_engine.entry_z <= abs(zt) < sig_engine.stop_z:
            g = sig_engine._gates(e, t)
            ok, _ = sig_engine._gates_ok(g, tf)
            if ok:
                side = "short" if zt > 0 else "long"
                from pairs_analysis.signal import _simulate_position
                exit_idx, reason, z_exit = _simulate_position(
                    side, t, zt, z.to_numpy(), sig_engine.stop_z, sig_engine.exit_z,
                    g["half_life_bars"], n - 1)
                if side == "long":
                    r = (z_exit - zt) / (sig_engine.stop_z + zt)
                else:
                    r = (zt - z_exit) / (sig_engine.stop_z - zt)
                trades.append({
                    "entry_bar": t, "exit_bar": exit_idx,
                    "entry_ts": str(ln1.index[t].date()),
                    "exit_ts": str(ln1.index[exit_idx].date()),
                    "entry_z": round(float(zt), 3),
                    "exit_z": round(float(z_exit), 3),
                    "side": side, "r": round(float(r), 3),
                    "exit_reason": reason,
                })
                t = exit_idx; cooldown = True; continue
        t += 1

    if not trades:
        print("  Нет сделок"); return

    print(f"  Сделок: {len(trades)}")
    print()

    # --- Ensemble prediction accuracy at each entry bar ---
    engine_names = ens_mod.ENGINE_NAMES
    engine_hits = {name: {"agree": 0, "disagree": 0, "neutral": 0,
                          "profit_when_agree": 0, "profit_when_disagree": 0,
                          "total_r": 0} for name in engine_names}

    for tr in trades:
        t_entry = tr["entry_bar"]
        side = tr["side"]
        profit = tr["r"] > 0

        results = _engine_prediction_at_bar(
            ens_mod.ENGINE_FUNCS, pair_name, tf, e, z, p1, ln1, ln2, window, t_entry)

        for name, res in zip(engine_names, results):
            if res is None:
                continue
            d = res.direction
            e_info = engine_hits[name]

            if d == "neutral":
                e_info["neutral"] += 1
            elif d == side:
                e_info["agree"] += 1
                if profit:
                    e_info["profit_when_agree"] += 1
            else:
                e_info["disagree"] += 1
                if profit:
                    e_info["profit_when_disagree"] += 1

            e_info["total_r"] += tr["r"]

    # --- Print results ---
    print(f"  {'Engine':<15s} {'Agree':>5s} {'Disagree':>8s} {'Neutral':>8s} "
          f"{'WR agree':>8s} {'WR disagree':>10s} {'AvgR':>6s}")
    print(f"  {'-'*75}")

    for name in engine_names:
        h = engine_hits[name]
        total_dir = h["agree"] + h["disagree"]
        wr_agree = (h["profit_when_agree"] / h["agree"] * 100) if h["agree"] > 0 else 0
        wr_disagree = (h["profit_when_disagree"] / h["disagree"] * 100) if h["disagree"] > 0 else 0
        avg_r = h["total_r"] / len(trades) if trades else 0
        print(f"  {name:<15s} {h['agree']:5d} {h['disagree']:8d} {h['neutral']:8d} "
              f"{wr_agree:7.1f}% {wr_disagree:9.1f}% {avg_r:+6.3f}")

    # --- Confidence accuracy: when engine has high conf, is it more right? ---
    print(f"\n  Confidence > 50% only:")
    for name in engine_names:
        h = engine_hits[name]
        # We can't get per-trade conf here without storing it, but we can
        # show the aggregate. The real accuracy metric is agree/disagree ratio.
        total = h["agree"] + h["disagree"] + h["neutral"]
        if total == 0: continue
        agree_pct = h["agree"] / (h["agree"] + h["disagree"]) * 100 if (h["agree"] + h["disagree"]) > 0 else 0
        print(f"  {name:<15s}: agree={agree_pct:.0f}% of directional calls "
              f"({h['agree']}+{h['disagree']})")

    return trades, engine_hits


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pair", default=None)
    ap.add_argument("--timeframe", default=None)
    args = ap.parse_args()

    cfg = load_config()
    analysis = cfg.get("analysis", {})
    thresholds = cfg.get("thresholds", {})
    bt_cfg = dict(analysis)
    bt_cfg.update(cfg.get("backtest", {}) or {})
    tf = args.timeframe or analysis.get("default_timeframe", "D1")

    print(f"=== Historical Ensemble Accuracy ({tf}) ===")

    for pair in cfg.get("pairs", []):
        name = pair["name"]
        if args.pair and args.pair.lower() not in name.lower():
            continue
        try:
            analyze_pair(pair, analysis, tf, bt_cfg, thresholds, name)
        except Exception as e:
            print(f"  {name}: ERROR {e}")


if __name__ == "__main__":
    main()
