"""
Per-trade diagnostic for ensemble signal quality.

Follow-up to the threshold sweep (2026-08-15): raising the confidence/probability
thresholds did not differentiate trades — the sweep showed almost identical
results for most variants. This script examines whether the raw ML probability
p_max, session, or regime splits actually separate profitable and unprofitable
trades for a given variant.

It uses the SAME walk-forward machinery as scripts/deflated_sharpe.py (purge/
embargo, per-fold model scoring) and the SAME EnsembleBacktester, so the analyzed
trades are exactly the ones behind the validation grid. No modifications to the
engine are required: we reconstruct per-trade features from the fold DataFrame
using the trade's entry_ts.

The script never touches the locked hold-out (it runs on data before --end-date,
same as the validation script), and it does not alter any config or model.

Output:
  logs/trade_quality_<asset>.csv — one row per trade
  console summary: overall, by p_max quartile, by session, by regime, top session/regime combos
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


def collect_trades_for_variant(cfg: dict, asset_key: str, df_full: pd.DataFrame,
                               variant_name: str, overrides: dict | None,
                               max_folds: int | None = None,
                               random_seed: int = 42) -> list[dict]:
    """Run all walk-forward folds for one variant and return per-trade records.

    Each record contains the fields needed for quality slicing:
      variant, entry_ts, session, regime, p_long, p_short, p_max, pnl, R,
      exit_reason, atr.
    """
    cfg_v = _apply_variant(cfg, asset_key, overrides)
    windows, frames = _build_fold_frames(df_full, cfg_v, asset_key, max_folds)

    bt_cfg = cfg_v.get("backtest", {})
    volume = bt_cfg.get("volume", 0.10)
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

        # Build a timestamp -> original row lookup for this fold.
        # fdf_run may be a copy (for null), but for current/wide it is the original frame.
        ts_series = fdf_run["timestamp_utc"].reset_index(drop=True)
        for t in trades:
            matches = ts_series[ts_series == t.entry_ts]
            if matches.empty:
                continue
            row_idx = matches.index[0]
            row = fdf_run.iloc[row_idx]

            p_long = float(row.get("ml_p_long", 0.5))
            p_short = float(row.get("ml_p_short", 0.5))
            p_max = max(p_long, p_short)
            risk_money = abs(t.entry_price - t.initial_stop_price) * t.volume * point_value_lot
            r = float(t.pnl / risk_money) if risk_money > 1e-12 else 0.0

            records.append({
                "variant": variant_name,
                "entry_ts": int(t.entry_ts),
                "session": t.session,
                "regime": t.regime_at_entry,
                "direction": t.direction,
                "p_long": round(p_long, 4),
                "p_short": round(p_short, 4),
                "p_max": round(p_max, 4),
                "pnl": round(float(t.pnl), 6),
                "R": round(r, 6),
                "exit_reason": t.exit_reason,
                "atr": float(row.get("atr", np.nan)),
            })
    return records


def _metrics_for(df_slice: pd.DataFrame) -> pd.Series:
    """Core aggregate metrics for a slice of trades."""
    n = len(df_slice)
    if n == 0:
        return pd.Series({"n": 0, "WR%": 0.0, "PF": 0.0, "PnL_R": 0.0, "R_mean": 0.0, "t_block": np.nan})
    r = df_slice["R"]
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
        "PnL_R": round(float(r.sum()), 3),
        "R_mean": round(float(r.mean()), 4),
        "t_block": round(t_block, 3) if not np.isnan(t_block) else np.nan,
    })


def summarize_records(df: pd.DataFrame) -> None:
    """Print per-variant summaries: overall, p_max quartiles, sessions, regimes, combos."""
    if df.empty:
        print("No trades collected.")
        return

    print(f"\nTrades: {len(df)}")
    print("Overall:", _metrics_for(df).to_dict())

    # Quartiles by p_max
    try:
        q = pd.qcut(df["p_max"], 4, duplicates="drop")
    except Exception:
        q = pd.cut(df["p_max"], 4)
    df = df.copy()
    df["p_max_q"] = q
    print("\nBy p_max quartile:")
    print(df.groupby("p_max_q", observed=True).apply(_metrics_for, include_groups=False).to_string())

    print("\nBy session:")
    print(df.groupby("session").apply(_metrics_for, include_groups=False).to_string())

    print("\nBy regime:")
    print(df.groupby("regime").apply(_metrics_for, include_groups=False).to_string())

    print("\nTop session x regime combinations (n >= 10):")
    comb = df.groupby(["session", "regime"]).size().reset_index(name="count").sort_values("count", ascending=False)
    shown = 0
    for _, row in comb.iterrows():
        sess, reg = row["session"], row["regime"]
        sub = df[(df["session"] == sess) & (df["regime"] == reg)]
        if len(sub) >= 10:
            print(f"{sess}/{reg}: {_metrics_for(sub).to_dict()}")
            shown += 1
        if shown >= 10:
            break


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Per-trade signal quality diagnostic.")
    parser.add_argument("--asset", required=True, help="Asset key (e.g. XAUUSD)")
    parser.add_argument("--db-path", default=None, help="SQLite DB path")
    parser.add_argument("--variants", default="current,wide",
                        help="Comma-separated variant names (must exist in the asset's variant family)")
    parser.add_argument("--end-date", default=None,
                        help="End date for data (same semantics as deflated_sharpe)")
    parser.add_argument("--max-folds", type=int, default=None, help="Cap folds for quick runs")
    parser.add_argument("--out-dir", default="logs", help="Directory for output CSV")
    args = parser.parse_args(argv)

    cfg = load_config()
    asset_key = args.asset
    if asset_key not in cfg.get("assets", {}):
        raise SystemExit(f"Unknown asset: {asset_key}")

    timeframe = cfg["assets"][asset_key].get("timeframe", "M15")
    db_path = args.db_path or cfg.get("general", {}).get("db_path")
    raw = load_asset_history(db_path, timeframe, asset_key)
    if args.end_date:
        raw = truncate_before(raw, args.end_date, asset_key)
    df_full = build_full_df(cfg, raw, db_path=db_path, asset_key=asset_key)

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    family = _variants_for(asset_key)

    all_records = []
    for vname in variants:
        if vname not in family:
            print(f"Warning: variant '{vname}' not in family for {asset_key}, skipping")
            continue
        overrides = family[vname]
        records = collect_trades_for_variant(cfg, asset_key, df_full, vname, overrides,
                                             max_folds=args.max_folds)
        all_records.extend(records)
        print(f"Collected {len(records)} trades for variant '{vname}'")

    if not all_records:
        print("No trades collected.")
        return

    combined = pd.DataFrame(all_records)
    os.makedirs(args.out_dir, exist_ok=True)
    out_csv = os.path.join(args.out_dir, f"trade_quality_{asset_key.lower()}.csv")
    combined.to_csv(out_csv, index=False)
    print(f"Saved raw per-trade data to {out_csv}")

    for vname in variants:
        sub = combined[combined["variant"] == vname]
        if sub.empty:
            continue
        print(f"\n================ Variant: {vname} ================")
        summarize_records(sub)


if __name__ == "__main__":
    main()