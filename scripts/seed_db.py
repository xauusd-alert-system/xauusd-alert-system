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
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.ingestion import backfill_historical
from data.storage import init_schema, upsert_candles
from data.session_tagger import tag_dataframe


def main():
    parser = argparse.ArgumentParser(description="Seed XAUUSD historical OHLCV data into SQLite.")
    parser.add_argument("--days", type=int, default=90, help="How many days back to seed (default: 90)")
    parser.add_argument("--timeframe", type=str, default=None, help="Single timeframe to seed (default: all)")
    args = parser.parse_args()

    cfg = load_config()
    db_path = cfg["general"]["db_path"]
    timeframes = [args.timeframe] if args.timeframe else cfg["timeframes"]
    sessions_cfg = cfg["sessions"]

    init_schema(db_path, timeframes)

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=args.days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    print(f"Seeding {len(timeframes)} timeframe(s) from {start_str} to {end_str} into {db_path}")

    for tf in timeframes:
        print(f"  [{tf}] fetching...", end=" ", flush=True)
        try:
            df = backfill_historical(
                timeframe=tf,
                start_date=start_str,
                end_date=end_str,
                sessions_config=sessions_cfg,
            )
            if df.empty:
                print("no data returned - skipping")
                continue
            upsert_candles(db_path, tf, df)
            print(f"{len(df)} candles written")
        except Exception as e:
            print(f"ERROR: {e}")

    print("Seed complete.")


if __name__ == "__main__":
    main()


