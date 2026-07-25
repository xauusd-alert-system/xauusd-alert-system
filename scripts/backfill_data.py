"""
One-time (or periodic) script to pull real historical XAUUSD data via Twelve Data
and store it locally for model training / backtest validation.

USAGE (run this on YOUR machine with internet access, not in this sandbox):

    export TWELVE_DATA_API_KEY=2ae4e2ce0dfa4f84a4c003ed1d3a0276
    python -m scripts.backfill_data --timeframe M15 --start 2024-01-01 --end 2026-07-24

This respects Twelve Data's free-tier rate limit (8 requests/minute) automatically
via data/ingestion.py::backfill_historical(). A ~2.5 year M15 pull is roughly
2.5*365*96 =~ 87,600 candles =~ 18 paginated requests at 5000 rows each =~ 2-3
minutes of wall-clock time given the throttle - safe to run on the free tier.
"""
import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.ingestion import backfill_historical
from data.storage import init_schema, upsert_candles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_data")


def main():
    parser = argparse.ArgumentParser(description="Backfill historical XAUUSD OHLCV data from Twelve Data.")
    parser.add_argument("--timeframe", default="M15", choices=["M1", "M5", "M15", "H1", "H4"])
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--db-path", default=None, help="Defaults to config.yaml general.db_path")
    args = parser.parse_args()

    cfg = load_config()
    db_path = args.db_path or cfg["general"]["db_path"]

    logger.info("Starting backfill: timeframe=%s start=%s end=%s", args.timeframe, args.start, args.end)
    logger.info("This may take a few minutes due to the free-tier 8 requests/minute rate limit.")

    df = backfill_historical(args.timeframe, args.start, args.end, cfg["sessions"])
    logger.info("Backfill complete: %d candles retrieved (%s to %s)",
                len(df), df["timestamp_utc"].min(), df["timestamp_utc"].max())

    init_schema(db_path, [args.timeframe])
    upsert_candles(db_path, args.timeframe, df)
    logger.info("Stored %d candles to %s (table ohlcv_%s)", len(df), db_path, args.timeframe.lower())


if __name__ == "__main__":
    main()
