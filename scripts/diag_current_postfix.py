"""One-off: re-run the 'current' XAUUSD walk-forward variant on the POST-FIX
(true-UTC + corrected session labels) DB, using the EXACT same path as the
pre-fix baseline (logs/ds_all_variants.csv, backed up as
logs/ds_all_variants_PRE_FIX.csv): train_mt5.build_full_df + shared fold frames
+ collect_direction_records. Writes logs/ds_current_postfix.csv in the same
schema so the two can be diffed row-for-row.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd

from config.loader import load_config
from scripts.deflated_sharpe import _variants_for
from scripts.diag_direction_split import collect_direction_records, _metrics_for
from scripts.run_backtest import load_asset_history, truncate_before
from scripts.train_mt5 import build_full_df

ASSET = "XAUUSD"
END_DATE = "2026-08-08"
VARIANT = "current"


def main() -> None:
    cfg = load_config()
    family = _variants_for(ASSET)
    if VARIANT not in family:
        raise SystemExit(f"Unknown variant {VARIANT}; available: {list(family)}")

    db = cfg.get("general", {}).get("db_path")
    timeframe = cfg["assets"][ASSET].get("timeframe", "M15")
    raw = load_asset_history(db, timeframe, ASSET)
    raw = truncate_before(raw, END_DATE, ASSET)
    df_full = build_full_df(raw, cfg, db_path=db, asset_key=ASSET, timeframe=timeframe)
    print(f"Loaded {len(df_full)} rows for {ASSET} ({timeframe}), end {END_DATE}", flush=True)

    overrides = family[VARIANT]
    records = collect_direction_records(cfg, ASSET, df_full, VARIANT, overrides)
    rec = pd.DataFrame(records) if records else pd.DataFrame()

    row = {"variant": VARIANT, "n_trades": len(rec)}
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

    out = pd.DataFrame([row])
    out_path = os.path.join("logs", "ds_current_postfix.csv")
    out.to_csv(out_path, index=False)
    print(f"WROTE {out_path}", flush=True)
    print(out.to_string(), flush=True)

    # Raw per-trade records for deeper comparison with the pre-fix file
    rec.to_csv(os.path.join("logs", "trade_quality_xauusd_dir_postfix.csv"), index=False)
    print(f"WROTE logs/trade_quality_xauusd_dir_postfix.csv ({len(rec)} trades)", flush=True)


if __name__ == "__main__":
    main()
