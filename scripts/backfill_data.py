"""
Backfill local FxPro MT5 candle history into the multi-asset SQLite database.

Examples:
    python -m scripts.backfill_data --all --timeframe M15 --start 2023-10-01 --end 2026-07-30
    python -m scripts.backfill_data --asset XAUUSD --timeframe M15 --start 2023-10-01 --end 2026-07-30
"""
import argparse
import logging
import os
import sys
from datetime import UTC, datetime, time

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.mt5_provider import fetch_candles_range, shutdown_mt5
from data.storage import init_schema, upsert_candles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill_data")


def _utc_bounds(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start = datetime.combine(
        datetime.strptime(start_date, "%Y-%m-%d").date(),
        time.min,
        tzinfo=UTC,
    )
    end = datetime.combine(
        datetime.strptime(end_date, "%Y-%m-%d").date(),
        time.max,
        tzinfo=UTC,
    )
    return start, end


def _session_label(timestamp: pd.Timestamp) -> str:
    """Tag session from a UTC timestamp.

    Saturday is always 'weekend' (FX market closed).
    Sunday 00:00-20:59 UTC is 'weekend' (market still closed).
    Sunday 21:00+ UTC is the start of the FX week — tag as the session
    that hour falls in (typically 'newyork' at 21-22 UTC, then 'asia' at
    00:00+ next day which is already Monday).  This matches the actual
    market reopen and prevents phantom 'weekend' trades in walk-forward.
    Weekdays use standard session windows.
    """
    weekday = timestamp.weekday()
    hour = timestamp.hour
    # Saturday: always weekend
    if weekday == 5:
        return "weekend"
    # Sunday: weekend before 21:00, real session after
    if weekday == 6 and hour < 21:
        return "weekend"
    # Sunday 21:00+ and all weekdays: classify by hour
    if 0 <= hour < 8:
        return "asia"
    if 8 <= hour < 13:
        return "london"
    # Issue #50: canonical name must match config sessions key (newyork).
    return "newyork"


def _to_storage_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()

    timestamp_column = next(
        (
            name
            for name in ("timestamp", "time", "datetime", "timestamp_utc")
            if name in frame.columns
        ),
        None,
    )
    if timestamp_column is None:
        raise ValueError(
            f"MT5 frame has no timestamp column. Columns: {list(frame.columns)}"
        )

    timestamps = pd.to_datetime(frame[timestamp_column], utc=True)
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    frame["timestamp_utc"] = (
        (timestamps - epoch).dt.total_seconds().astype("int64")
    )

    if "volume" not in frame.columns:
        if "tick_volume" in frame.columns:
            frame["volume"] = frame["tick_volume"]
        elif "real_volume" in frame.columns:
            frame["volume"] = frame["real_volume"]
        else:
            frame["volume"] = 0.0

    frame["session"] = timestamps.map(_session_label)

    required = [
        "timestamp_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "session",
    ]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"MT5 frame missing columns: {sorted(missing)}")

    return (
        frame[required]
        .drop_duplicates(subset=["timestamp_utc"], keep="last")
        .sort_values("timestamp_utc")
        .reset_index(drop=True)
    )



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill enabled assets from the locally running FxPro MT5 terminal."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--asset", help="Internal asset key, for example XAUUSD")
    target.add_argument("--all", action="store_true", help="Backfill every enabled asset")
    parser.add_argument(
        "--timeframe",
        default=None,
        choices=["M1", "M5", "M15", "H1", "H4"],
        help="Defaults to config market_data.timeframe",
    )
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument(
        "--db-path",
        default="data/market_data_mt5.sqlite",
        help="SQLite destination path",
    )
    args = parser.parse_args()

    start_utc, end_utc = _utc_bounds(args.start, args.end)
    if end_utc <= start_utc:
        raise SystemExit("--end must be on or after --start.")

    cfg = load_config()
    market_data = cfg.get("market_data", {})
    if market_data.get("provider") != "mt5":
        raise SystemExit("config/config.yaml must specify market_data.provider: mt5")
    global_tf = args.timeframe or market_data.get("timeframe", "M5")
    assets = cfg.get("assets", {})

    if args.all:
        selected = {
            asset_key: asset_cfg
            for asset_key, asset_cfg in assets.items()
            if asset_cfg.get("enabled", False)
        }
    else:
        if args.asset not in assets:
            raise SystemExit(f"Unknown asset key: {args.asset}")
        if not assets[args.asset].get("enabled", False):
            raise SystemExit(f"Asset is disabled: {args.asset}")
        selected = {args.asset: assets[args.asset]}

    if not selected:
        raise SystemExit("No enabled assets selected.")

    # Resolve per-asset timeframe: asset override -> global -> M5 fallback.
    # Without this, --all would backfill EURUSD (H1) with M5 candles.
    def _resolve_tf(asset_cfg: dict) -> str:
        return asset_cfg.get("timeframe") or global_tf

    tfs_needed = sorted({_resolve_tf(ac) for ac in selected.values()})
    init_schema(args.db_path, tfs_needed)

    try:
        for asset_key, asset_cfg in selected.items():
            timeframe = _resolve_tf(asset_cfg)
            mt5_symbol = asset_cfg["mt5_symbol"]

            logger.info(
                "Fetching %s (%s), %s, %s through %s",
                asset_key,
                mt5_symbol,
                timeframe,
                args.start,
                args.end,
            )

            raw = fetch_candles_range(
                symbol=mt5_symbol,
                timeframe=timeframe,
                start_utc=start_utc,
                end_utc=end_utc,
            )

            stored = _to_storage_frame(raw)
            if stored.empty:
                raise RuntimeError(f"{asset_key}: MT5 returned no candles.")

            upsert_candles(args.db_path, timeframe, asset_key, stored)

            first = pd.to_datetime(stored["timestamp_utc"].iloc[0], unit="s", utc=True)
            last = pd.to_datetime(stored["timestamp_utc"].iloc[-1], unit="s", utc=True)

            logger.info(
                "%s: stored %d candles (%s through %s)",
                asset_key,
                len(stored),
                first.isoformat(),
                last.isoformat(),
            )
    finally:
        shutdown_mt5()


if __name__ == "__main__":
    main()
