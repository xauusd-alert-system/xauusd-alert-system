"""Sweep ensemble.min_edge over the walk-forward for XAUUSD / EURUSD.

The deployed models produce compressed probabilities (p_long in ~0.49-0.55),
so the min_edge=0.15 gate (|p_long - p_short| >= 0.15 <-> p_max >= 0.575 with
normalized probs) fires almost nothing. This script re-runs the SAME
walk-forward used by run_backtest (same folds, same per-fold retrained
models, same entry/exit geometry) with min_edge lowered to 0.10 / 0.08 and
counts how many more signals fire and what their expectancy is.

NOTE on the gate chain: min_ml_probability=0.55 (ensemble config) is a
separate floor. min_edge=0.10 <-> p_max >= 0.55 exactly, so below 0.10 the
min_ml_probability floor becomes the binding constraint unless it is also
lowered.

Standalone (untracked) script: reuses run_backtest's strategy machinery
without editing tracked files.

Usage:
    python -m scripts.diag_min_edge_sweep [--assets XAUUSD,EURUSD] [--edges 0.15,0.10,0.08]
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtest.walk_forward import generate_windows, run_walk_forward  # noqa: E402
from config.loader import load_config  # noqa: E402
from data.provenance import provenance_gate  # noqa: E402
from scripts.run_backtest import (  # noqa: E402
    build_full_df,
    load_asset_history,
    strategy_fn_factory,
    truncate_before,
)
from scripts.trial_journal import enforce_locked_holdout  # noqa: E402

END_DATE = "2026-08-08"  # locked hold-out start: research never burns it


def run_asset_sweep(
    asset: str,
    edges: list[float],
    min_prob: float | None = None,
    min_conf: float | None = None,
    relax_sessions: bool = False,
    relax_regimes: bool = False,
) -> pd.DataFrame:
    cfg = load_config()
    asset_cfg = cfg["assets"][asset]
    db = cfg["general"]["db_path"]
    model_path = asset_cfg["model_path"]
    timeframe = asset_cfg.get("timeframe") or cfg.get("market_data", {}).get("timeframe", "M5")

    raw = load_asset_history(db, timeframe, asset)
    provenance_gate(cfg, db, timeframe, asset)
    raw = truncate_before(raw, END_DATE, asset)
    df = build_full_df(cfg, raw, db_path=db, asset_key=asset)
    print(f"[{asset}] loaded {len(df)} rows (tf={timeframe}), lock {END_DATE}", flush=True)

    wf = cfg["backtest"]["walk_forward"]
    windows = generate_windows(df, wf["train_window_days"], wf["test_window_days"], wf["step_days"])
    enforce_locked_holdout(cfg, windows, "min_edge_sweep", allow=False)

    rows = []
    for edge in edges:
        # A fresh deep copy so each threshold run is independent; the fold
        # models are identical across thresholds (min_edge only gates the
        # TEST-phase signal), so differences are purely the gate.
        import copy

        cfg_e = copy.deepcopy(cfg)
        cfg_e.setdefault("ensemble", {})["min_edge"] = edge
        if min_prob is not None:
            cfg_e["ensemble"]["min_ml_probability"] = min_prob
        if min_conf is not None:
            cfg_e["ensemble"]["min_confidence_to_alert"] = min_conf
        if relax_sessions:
            cfg_e["ensemble"]["suppress_sessions"] = []
        if relax_regimes:
            cfg_e["ensemble"]["suppress_regimes"] = []
        results = run_walk_forward(df, cfg_e, strategy_fn_factory(cfg_e, model_path, asset_key=asset))
        agg = _aggregate(results)
        agg["asset"] = asset
        agg["min_edge"] = edge
        rows.append(agg)
        print(
            f"[{asset}] min_edge={edge}: {agg['n_trades']} trades, "
            f"total_pnl={agg['total_pnl']:+.2f}, pnl/trade={agg['pnl_per_trade']:+.4f}, "
            f"medianPF={agg['median_pf']:.3f}, pos_folds={agg['pos_folds']}/{agg['valid_folds']}",
            flush=True,
        )
    return pd.DataFrame(rows)


def _aggregate(results: list[dict]) -> dict:
    valid = [r for r in results if r.get("n_trades", 0) > 0]
    if not valid:
        return {
            "n_trades": 0,
            "total_pnl": 0.0,
            "pnl_per_trade": 0.0,
            "median_pf": 0.0,
            "win_rate": 0.0,
            "pos_folds": 0,
            "valid_folds": 0,
        }
    n = int(sum(r["n_trades"] for r in valid))
    pnl = float(sum(r.get("total_pnl", 0.0) for r in valid))
    pfs = [
        r["profit_factor"]
        for r in valid
        if r.get("profit_factor", 0) is not None and r["profit_factor"] == r["profit_factor"]
    ]
    import numpy as np

    return {
        "n_trades": n,
        "total_pnl": round(pnl, 2),
        "pnl_per_trade": round(pnl / n, 4) if n else 0.0,
        "median_pf": round(float(np.median(pfs)), 3) if pfs else 0.0,
        "win_rate": round(float(np.mean([r["win_rate"] for r in valid])), 1),
        "pos_folds": sum(1 for r in valid if r.get("total_pnl", 0.0) > 0),
        "valid_folds": len(valid),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="XAUUSD,EURUSD")
    ap.add_argument("--edges", default="0.15,0.10,0.08")
    ap.add_argument(
        "--min-prob", type=float, default=None, help="Override ensemble.min_ml_probability (probe the binding gate)"
    )
    ap.add_argument(
        "--min-conf",
        type=float,
        default=None,
        help="Override ensemble.min_confidence_to_alert (probe the binding gate)",
    )
    ap.add_argument(
        "--relax-sessions", action="store_true", help="Disable session suppression (probe the binding filter)"
    )
    ap.add_argument(
        "--relax-regimes", action="store_true", help="Disable regime suppression (probe the binding filter)"
    )
    ap.add_argument("--out", default="logs/min_edge_sweep.csv")
    args = ap.parse_args(argv)

    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    edges = [float(e) for e in args.edges.split(",") if e.strip()]
    frames = []
    for asset in assets:
        frames.append(
            run_asset_sweep(
                asset,
                edges,
                min_prob=args.min_prob,
                min_conf=args.min_conf,
                relax_sessions=args.relax_sessions,
                relax_regimes=args.relax_regimes,
            )
        )

    out = pd.concat(frames, ignore_index=True)
    os.makedirs("logs", exist_ok=True)
    out.to_csv(args.out, index=False)
    print("\n========== MIN-EDGE SWEEP SUMMARY ==========")
    cols = [
        "asset",
        "min_edge",
        "n_trades",
        "total_pnl",
        "pnl_per_trade",
        "win_rate",
        "median_pf",
        "pos_folds",
        "valid_folds",
    ]
    print(out[cols].to_string(index=False))
    print(f"\nCSV -> {args.out}")


if __name__ == "__main__":
    main()
