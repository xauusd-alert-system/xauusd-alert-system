"""Pre/post-fix walk-forward comparison for BTCUSD and EURUSD.

Reproduces the pre-fix regime from the current true-UTC DB the way the OLD
backfill stored it: timestamps shifted +3h (FxPro server time stored as UTC)
and sessions labeled with the old backfill scheme (asia 0-8, london 8-13,
newyork 13-24, NO off_session; weekend by shifted date).

Because build_full_df pulls H1/H4 for MTF confluence via
scripts.train_mt5.read_candles, the wrapper shifts those frames too — in the
pre-fix DB the HTF tables were shifted the same way, so both M15 and HTF stay
aligned exactly as they were.

Runs the SAME honest walk-forward machinery as diag_current_postfix.py:
train_mt5.build_full_df + shared fold frames + collect_direction_records
(variant 'current', per-fold fresh XGBoost + calibration).

Writes:
  logs/dir_prepost_<asset>_postfix.csv  (per-trade records, true UTC)
  logs/dir_prepost_<asset>_prefix.csv   (per-trade records, +3h shift)
and prints the long/short comparison table.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from scripts.deflated_sharpe import _variants_for
from scripts.diag_direction_split import collect_direction_records, _metrics_for
from scripts.run_backtest import load_asset_history, truncate_before
import scripts.train_mt5 as train_mt5

END_DATE = "2026-08-08"
SHIFT_H = 3
ASSETS = ["BTCUSD", "EURUSD"]  # override via --assets XAGUSD


def old_session_label(ts: pd.Timestamp) -> str:
    """Exact pre-fix backfill scheme (git show HEAD:scripts/backfill_data.py)."""
    if ts.weekday() >= 5:
        return "weekend"
    h = ts.hour
    if h < 8:
        return "asia"
    if h < 13:
        return "london"
    return "newyork"


def _shift_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["timestamp_utc"] = out["timestamp_utc"].astype("int64") + SHIFT_H * 3600
    if "timestamp" in out.columns:
        out["timestamp"] = pd.to_datetime(out["timestamp_utc"], unit="s", utc=True)
    if "session" in out.columns:
        out["session"] = out["timestamp"].map(old_session_label)
    return out


def run_asset(cfg: dict, asset: str, timeframe: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    family = _variants_for(asset)
    overrides = family["current"]
    db = cfg["general"]["db_path"]
    # Explicit --timeframe wins (e.g. production tier M5); otherwise the asset
    # config value; else M15 (the historical research default).
    timeframe = timeframe or cfg["assets"][asset].get("timeframe", "M15") or "M15"
    print(f"timeframe={timeframe}", flush=True)

    raw = load_asset_history(db, timeframe, asset)

    # ---- POST-FIX: true UTC + corrected sessions (current DB) ----
    raw_post = truncate_before(raw, END_DATE, asset)
    df_post = train_mt5.build_full_df(
        raw_post, cfg, db_path=db, asset_key=asset, timeframe=timeframe)
    rec_post = collect_direction_records(cfg, asset, df_post, "current", overrides)

    # ---- PRE-FIX reproduction: +3h shift + old session labels (M15 AND HTF) ----
    orig_read_candles = train_mt5.read_candles

    def shifted_read_candles(db_path, tf, symbol, start_ts=None, end_ts=None):
        frame = orig_read_candles(db_path, tf, symbol, start_ts=start_ts, end_ts=end_ts)
        if frame.empty:
            return frame
        frame = _shift_frame(frame)
        return frame

    train_mt5.read_candles = shifted_read_candles
    try:
        raw_pre = _shift_frame(raw)
        raw_pre = truncate_before(raw_pre, END_DATE, asset)
        df_pre = train_mt5.build_full_df(
            raw_pre, cfg, db_path=db, asset_key=asset, timeframe=timeframe)
    finally:
        train_mt5.read_candles = orig_read_candles
    rec_pre = collect_direction_records(cfg, asset, df_pre, "current", overrides)

    return pd.DataFrame(rec_pre), pd.DataFrame(rec_post)


def summarize(rec: pd.DataFrame) -> dict:
    row = {"n_trades": len(rec)}
    if rec.empty:
        return row
    for prefix, sub in (("short", rec[rec["direction"] == "short"]),
                        ("long", rec[rec["direction"] == "long"])):
        m = _metrics_for(sub)
        row[f"{prefix}_n"] = int(m["n"])
        row[f"{prefix}_WR"] = float(m["WR%"])
        row[f"{prefix}_PF"] = float(m["PF"])
        row[f"{prefix}_sumR"] = float(m["sum_R"])
        if len(sub):
            row[f"{prefix}_folds"] = sorted(sub["fold_id"].unique())
    tot = _metrics_for(rec)
    row["total_sumR"] = float(tot["sum_R"])
    row["total_PF"] = float(tot["PF"])
    return row


def main(argv: list[str] | None = None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Pre/post-fix walk-forward comparison.")
    parser.add_argument("--assets", nargs="+", default=ASSETS,
                        help="Asset keys to compare (default: %(default)s)")
    parser.add_argument("--timeframe", default=None,
                        help="Override setup timeframe for ALL assets "
                             "(e.g. M5 = production tier). Default: per-asset "
                             "config value, falling back to M15.")
    args = parser.parse_args(argv)

    cfg = load_config()
    out_dir = os.path.join("logs")
    tf_suffix = f"_{args.timeframe.lower()}" if args.timeframe else ""
    rows = []
    for asset in args.assets:
        print(f"\n########## {asset} ##########", flush=True)
        rec_pre, rec_post = run_asset(cfg, asset, timeframe=args.timeframe)

        rec_pre.to_csv(os.path.join(out_dir, f"dir_prepost_{asset.lower()}{tf_suffix}_prefix.csv"), index=False)
        rec_post.to_csv(os.path.join(out_dir, f"dir_prepost_{asset.lower()}{tf_suffix}_postfix.csv"), index=False)
        print(f"wrote {asset}: pre n={len(rec_pre)}, post n={len(rec_post)}", flush=True)

        pre = summarize(rec_pre)
        post = summarize(rec_post)
        rows.append({"asset": asset, "regime": f"PRE_FIX(+3h){tf_suffix}", **pre})
        rows.append({"asset": asset, "regime": f"POST_FIX(true UTC){tf_suffix}", **post})

    print("\n========== SUMMARY ==========")
    df = pd.DataFrame(rows)
    cols = [c for c in df.columns if c != "asset"]
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
