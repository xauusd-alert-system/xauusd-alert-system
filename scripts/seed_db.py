"""
One-time (or periodic) historical data seed script.
Pulls OHLCV candles for all configured timeframes from Twelve Data API
and writes them to the local SQLite database.

Usage:
    python scripts/seed_db.py                        # seeds all timeframes, last 90 days
    python scripts/seed_db.py --days 180             # last 180 days
    python scripts/seed_db.py --timeframe M15        # single timeframe only

Requires env var: TWELVEDATA_API_KEY
"""
import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.ingestion import backfill_historical
from data.storage import init_schema, upsert_candles


def main():
    parser = argparse.ArgumentParser(description="Seed historical OHLCV data into SQLite.")
    parser.add_argument("--days", type=int, default=90, help="How many days back to seed (default: 90)")
    parser.add_argument("--timeframe", type=str, default=None, help="Single timeframe to seed (default: all)")
    parser.add_argument("--symbol", type=str, default="XAUUSD",
                        help="Asset key from config (default: XAUUSD)")
    args = parser.parse_args()

    cfg = load_config()
    db_path = cfg["general"]["db_path"]
    symbol = args.symbol
    # Default timeframe list: the primary market timeframe plus the MTF references
    # plus any per-asset timeframe overrides (e.g. EURUSD/GBPUSD/XAGUSD on M15),
    # so seeded data covers both the main pipeline and the higher-timeframe features.
    default_timeframes = [cfg["market_data"]["timeframe"]]
    default_timeframes += cfg.get("features", {}).get("mtf_reference_timeframes", [])
    default_timeframes += [
        a_cfg.get("timeframe")
        for a_cfg in cfg.get("assets", {}).values()
        if a_cfg.get("enabled", False) and a_cfg.get("timeframe")
    ]
    default_timeframes = sorted({tf for tf in default_timeframes if tf})
    timeframes = [args.timeframe] if args.timeframe else default_timeframes
    sessions_cfg = cfg["sessions"]

    init_schema(db_path, timeframes)

    end_date = datetime.now(UTC)
    start_date = end_date - timedelta(days=args.days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    print(f"Seeding {len(timeframes)} timeframe(s) for {symbol} from {start_str} to {end_str} into {db_path}")

    asset_cfg = cfg.get("assets", {}).get(symbol, {})
    api_symbol = asset_cfg.get("display_name", "XAU/USD")

    for tf in timeframes:
        print(f"  [{tf}/{symbol}] fetching...", end=" ", flush=True)
        try:
            df = backfill_historical(
                symbol=api_symbol,
                timeframe=tf,
                start_date=start_str,
                end_date=end_str,
                sessions_config=sessions_cfg,
            )
            if df.empty:
                print("no data returned - skipping")
                continue
            upsert_candles(db_path, tf, symbol, df)
            print(f"{len(df)} candles written")
        except Exception as e:
            print(f"ERROR: {e}")

    print("Seed complete.")


if __name__ == "__main__":
    main()


