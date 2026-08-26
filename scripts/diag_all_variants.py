"""Run diag_direction_split internals across all XAUUSD variants and write a
per-variant long/short summary CSV (logs/ds_all_variants.csv). One process,
reuses collect_direction_records from scripts/diag_direction_split.py so it is
bit-identical to the per-variant CLI run on the SHARED honest walk-forward.
"""
import os, sys, json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pandas as pd

from config.loader import load_config
from scripts.deflated_sharpe import _variants_for
from scripts.diag_direction_split import collect_direction_records, _metrics_for
from scripts.run_backtest import load_asset_history, truncate_before
from scripts.train_mt5 import build_full_df

def main():
    asset = "XAUUSD"
    end_date = "2026-08-08"
    cfg = load_config()
    family = _variants_for(asset)
    # null is the negative control - skip for direction economics
    variants = [k for k in family if k != "null"]

    db = cfg.get("general", {}).get("db_path")
    timeframe = cfg["assets"][asset].get("timeframe", "M15")
    raw = load_asset_history(db, timeframe, asset)
    if end_date:
        raw = truncate_before(raw, end_date, asset)
    df_full = build_full_df(raw, cfg, db_path=db, asset_key=asset, timeframe=timeframe)
    print(f"Loaded {len(df_full)} rows", flush=True)

    rows = []
    for vname in variants:
        overrides = family[vname]
        records = collect_direction_records(cfg, asset, df_full, vname, overrides)
        rec = pd.DataFrame(records) if records else pd.DataFrame()
        row = {"variant": vname, "n_trades": len(rec)}
        if not rec.empty:
            short = rec[rec["direction"] == "short"]
            long_ = rec[rec["direction"] == "long"]
            for prefix, sub in (("short", short), ("long", long_)):
                m = _metrics_for(sub)
                row[f"{prefix}_n"] = int(m["n"])
                row[f"{prefix}_WR"] = float(m["WR%"])
                row[f"{prefix}_PF"] = float(m["PF"])
                row[f"{prefix}_sumR"] = float(m["sum_R"])
                row[f"{prefix}_Rmean"] = float(m["R_mean"])
                if len(sub):
                    row[f"{prefix}_folds"] = sorted(sub["fold_id"].unique())
            tot = _metrics_for(rec)
            row["total_sumR"] = float(tot["sum_R"])
            row["total_PF"] = float(tot["PF"])
        rows.append(row)
        print(f"done {vname}: n={row['n_trades']} short_sumR={row.get('short_sumR')}", flush=True)

    out = pd.DataFrame(rows)
    out_path = os.path.join("logs", "ds_all_variants.csv")
    out.to_csv(out_path, index=False)
    print(f"WROTE {out_path}", flush=True)
    # console summary - sort by short sumR (least negative = best for shorts)
    cols = [c for c in out.columns if c in ("variant","n_trades","short_n","short_WR","short_PF","short_sumR","short_Rmean","long_sumR","total_sumR","total_PF")]
    print("\n=== By short_sumR (best for shorts first) ===")
    print(out.sort_values("short_sumR", ascending=False, na_position="last")[cols].to_string(), flush=True)

if __name__ == "__main__":
    main()