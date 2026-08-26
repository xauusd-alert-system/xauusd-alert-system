"""
Per-direction (long vs short) walk-forward diagnostics for one asset.

Runs the SAME honest walk-forward machinery as scripts/deflated_sharpe.py /
scripts/diag_trade_quality.py (purge/embargo, per-fold FRESH XGBoost +
calibration via model.trainer, same EnsembleBacktester) and splits the
resulting out-of-sample trades by direction, so long and short can be
compared on expectancy.

Motivation (2026-08-25): XAUUSD calibration fix. The old calibration
sigmoid was fit on 100 rows (0.2% of the fit set) and collapsed the
probability spread, which made shorts mathematically impossible on the
deployed model (0/400 live bars). After retraining with the fixed
calibration, this script verifies shorts now trade at all in OOS
walk-forward and whether they carry positive expectancy.

IMPORTANT honesty note: this scores per-fold freshly trained models (the
harness NEVER reads the production model file, HIGH-11). What it measures is
the fixed training/calibration PIPELINE out of sample — which is also what
the retrained production model was built with, so the short-side behaviour
is representative. The production model itself is loaded by the live trader,
not by this script.

The script never touches the locked hold-out (default --end-date is the
hold-out start), and it does not alter any config or model.

Output:
  logs/trade_quality_<asset>_dir.csv  — raw per-trade records (one row per
                                        trade, with direction and fold_id)
  logs/direction_split_<asset>.csv    — per-direction summary rows
  console: overall + long vs short comparison, fold-level verdicts
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.deflated_sharpe import (
    _apply_variant,
    _prepare_fold_frame,
    _build_fold_frames,
    _variants_for,
)
from scripts.run_backtest import (
    load_asset_history,
    build_full_df,
    merge_asset_cfg,
    truncate_before,
)
from model.ensemble_backtest import EnsembleBacktester
from backtest.metrics import block_bootstrap_t


def _direction_label(d: object) -> str:
    s = str(d).lower()
    if s.startswith("long") or s in ("l", "buy", "1", "+1"):
        return "long"
    if s.startswith("short") or s in ("s", "sell", "-1"):
        return "short"
    return s


def collect_direction_records(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                              variant_name: str, overrides: dict | None,
                              max_folds: int | None = None,
                              random_seed: int = 42) -> list[dict]:
    """Run all walk-forward folds for one variant and return per-trade records
    tagged with direction AND fold_id (so per-direction fold verdicts can be
    computed). Same helpers as diag_trade_quality.collect_trades_for_variant."""
    cfg_v = _apply_variant(cfg, asset_key, overrides)
    windows, frames = _build_fold_frames(df_full, cfg_v, asset_key, max_folds)

    bt_cfg = cfg_v.get("backtest", {})
    point_value_lot = cfg_v.get("assets", {}).get(asset_key, {}).get(
        "point_value_lot", bt_cfg.get("point_value_lot", 100.0)
    )

    records = []
    for fold_i, fdf in enumerate(frames):
        fdf_run = _prepare_fold_frame(fdf, variant_name, fold_i, random_seed)
        cfg_run = merge_asset_cfg(cfg_v, asset_key, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset_key, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset_key)
        trades = engine.run(fdf_run.reset_index(drop=True))

        if not trades:
            continue

        ts_series = fdf_run["timestamp_utc"].reset_index(drop=True)
        for t in trades:
            matches = ts_series[ts_series == t.entry_ts]
            if matches.empty:
                continue
            row_idx = matches.index[0]
            row = fdf_run.iloc[row_idx]

            p_long = float(row.get("ml_p_long", 0.5))
            p_short = float(row.get("ml_p_short", 0.5))
            risk_money = abs(t.entry_price - t.initial_stop_price) * t.volume * point_value_lot
            r = float(t.pnl / risk_money) if risk_money > 1e-12 else 0.0

            records.append({
                "fold_id": fold_i,
                "variant": variant_name,
                "entry_ts": int(t.entry_ts),
                "direction": _direction_label(t.direction),
                "session": t.session,
                "regime": t.regime_at_entry,
                "p_long": round(p_long, 4),
                "p_short": round(p_short, 4),
                "p_max": round(max(p_long, p_short), 4),
                "pnl": round(float(t.pnl), 6),
                "R": round(r, 6),
                "exit_reason": t.exit_reason,
            })
    return records


def _metrics_for(df_slice: pd.DataFrame) -> pd.Series:
    n = len(df_slice)
    if n == 0:
        return pd.Series({"n": 0, "WR%": 0.0, "PF": 0.0, "sum_R": 0.0, "R_mean": 0.0,
                          "R_median": 0.0, "sum_pnl": 0.0, "t_block": np.nan})
    r = df_slice["R"].astype(float)
    wins = r[r > 0]
    losses = r[r <= 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    pf = (gp / gl) if gl > 0 else 999.0
    wr = 100.0 * len(wins) / n
    t_block = block_bootstrap_t(r.tolist()) if n >= 2 else np.nan
    return pd.Series({
        "n": n,
        "WR%": round(wr, 1),
        "PF": round(pf, 2) if pf != 999.0 else 999.0,
        "sum_R": round(float(r.sum()), 3),
        "R_mean": round(float(r.mean()), 4),
        "R_median": round(float(r.median()), 4),
        "sum_pnl": round(float(df_slice["pnl"].sum()), 2),
        "t_block": round(t_block, 3) if not np.isnan(t_block) else np.nan,
    })


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Long vs short walk-forward diagnostics.")
    parser.add_argument("--asset", required=True, help="Asset key (e.g. XAUUSD)")
    parser.add_argument("--db-path", default=None, help="SQLite DB path")
    parser.add_argument("--variant", default="current",
                        help="Variant name (must exist in the asset's variant family)")
    parser.add_argument("--end-date", default="2026-08-08",
                        help="Drop candles at or after this UTC date. Default is the "
                             "locked hold-out start: research never burns the hold-out.")
    parser.add_argument("--max-folds", type=int, default=None, help="Cap folds for quick runs")
    parser.add_argument("--out-dir", default="logs", help="Directory for output CSVs")
    args = parser.parse_args(argv)

    cfg = load_config()
    asset_key = args.asset
    if asset_key not in cfg.get("assets", {}):
        raise SystemExit(f"Unknown asset: {asset_key}")

    family = _variants_for(asset_key)
    if args.variant not in family:
        raise SystemExit(f"Unknown variant '{args.variant}'; available: {list(family)}")

    timeframe = cfg["assets"][asset_key].get("timeframe", "M15")
    db_path = args.db_path or cfg.get("general", {}).get("db_path")
    raw = load_asset_history(db_path, timeframe, asset_key)
    if args.end_date:
        raw = truncate_before(raw, args.end_date, asset_key)
    df_full = build_full_df(cfg, raw, db_path=db_path, asset_key=asset_key)
    print(f"Loaded {len(df_full)} rows for {asset_key} ({timeframe}), end {args.end_date}")

    records = collect_direction_records(cfg, asset_key, df_full, args.variant,
                                        family[args.variant], max_folds=args.max_folds)
    if not records:
        print("No trades collected.")
        return

    combined = pd.DataFrame(records)
    os.makedirs(args.out_dir, exist_ok=True)
    raw_csv = os.path.join(args.out_dir, f"trade_quality_{asset_key.lower()}_dir.csv")
    combined.to_csv(raw_csv, index=False)
    print(f"Saved raw per-trade data to {raw_csv}")

    print(f"\n=== {asset_key} walk-forward, variant '{args.variant}' ===")
    print(f"Trades: {len(combined)} | folds with trades: "
          f"{combined['fold_id'].nunique()}/{combined['fold_id'].max() + 1}")
    print("Overall: ", _metrics_for(combined).to_dict())

    print("\n--- By direction ---")
    by_dir = combined.groupby("direction", observed=True).apply(
        _metrics_for, include_groups=False).reset_index()
    print(by_dir.to_string(index=False))

    # Exit-reason breakdown per direction
    print("\n--- Exit reasons by direction ---")
    xtab = pd.crosstab(combined["direction"], combined["exit_reason"])
    print(xtab.to_string())

    # Fold-level verdict per direction: shorts must make money in more folds
    # than they lose, not just in aggregate.
    print("\n--- Fold-level verdicts per direction ---")
    verdict_rows = []
    for direction, sub in combined.groupby("direction", observed=True):
        folds = sorted(sub["fold_id"].unique())
        pos = sum(sub[sub["fold_id"] == f]["R"].sum() > 0 for f in folds)
        verdict_rows.append({
            "direction": direction,
            "folds_traded": len(folds),
            "folds_positive": pos,
            "folds_positive_pct": round(100.0 * pos / len(folds), 1) if folds else 0.0,
        })
    print(pd.DataFrame(verdict_rows).to_string(index=False))

    summary = by_dir.merge(pd.DataFrame(verdict_rows), on="direction", how="left")
    out_csv = os.path.join(args.out_dir, f"direction_split_{asset_key.lower()}.csv")
    summary.to_csv(out_csv, index=False)
    print(f"\nSaved per-direction summary to {out_csv}")


if __name__ == "__main__":
    main()
