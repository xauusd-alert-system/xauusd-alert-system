"""
One-off: rebuild the historical ohlcv_* tables on TRUE UTC.

Background (2026-08-25): the DB was backfilled with MT5 bar timestamps in
BROKER-SERVER time (FxPro = UTC+3 EEST) declared as UTC. This driver re-fetches
every existing (timeframe, symbol) combination through fetch_candles_range with
server_time_offset_hours=3 (from config market_data.server_time_offset_hours),
so stored timestamp_utc values and session labels are true UTC. The previous
content of each (table, symbol) is DELETED before the fresh insert (upsert alone
would leave the old broker-time rows in place).

Safety:
  * Requires a manual DB backup before running (the caller is responsible).
  * A symbol is only deleted AFTER a successful non-empty fetch.
  * Only ohlcv_* tables are touched; executed_trades / ledger_* are untouched.
  * spread/real_volume (previously dropped) are preserved from the broker frame.

Usage:
    python scripts/rebuild_db_utc.py [--start 2019-12-01] [--end 2026-08-26]
"""
import argparse
import logging
import os
import sys
from datetime import datetime, time, timezone

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.mt5_provider import fetch_candles_range, shutdown_mt5, resolve_server_offset
from data.session_tagger import tag_session_with_weekend
from data.storage import init_schema, upsert_candles, get_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rebuild_db_utc")

# (table, symbol) combos that exist today. M5 spans back to 2020, M15/H1 to 2024.
COMBO_STARTS = {
    "M5": "2019-12-01",
    "M15": "2023-12-01",
    "H1": "2023-12-01",
}


def _to_storage_frame(df: pd.DataFrame, sessions_config: dict) -> pd.DataFrame:
    """Same contract as backfill_data._to_storage_frame, but PRESERVES the
    broker spread / real_volume columns instead of dropping them."""
    frame = df.copy()
    ts_col = next((c for c in ("timestamp", "time", "datetime", "timestamp_utc") if c in frame.columns), None)
    if ts_col is None:
        raise ValueError(f"No timestamp column; got {list(frame.columns)}")
    timestamps = pd.to_datetime(frame[ts_col], utc=True)
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    frame["timestamp_utc"] = (timestamps - epoch).dt.total_seconds().astype("int64")
    if "volume" not in frame.columns:
        frame["volume"] = frame.get("tick_volume", frame.get("real_volume", 0.0))
    # Canonical storage label: weekend excluded, otherwise config windows
    # (off_session outside asia/london/newyork) — matches data.session_tagger.
    frame["session"] = timestamps.map(lambda t: tag_session_with_weekend(t, sessions_config))
    out_cols = ["timestamp_utc", "open", "high", "low", "close", "volume", "session"]
    for opt in ("spread", "real_volume"):
        if opt in frame.columns:
            frame[opt] = pd.to_numeric(frame[opt], errors="coerce")
            out_cols.append(opt)
    missing = {"timestamp_utc", "open", "high", "low", "close", "volume", "session"} - set(frame.columns)
    if missing:
        raise ValueError(f"frame missing: {sorted(missing)}")
    return (
        frame[out_cols]
        .drop_duplicates(subset=["timestamp_utc"], keep="last")
        .sort_values("timestamp_utc")
        .reset_index(drop=True)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--start", default=None, help="Override start (YYYY-MM-DD) for all combos")
    parser.add_argument("--end", default="2026-08-26", help="End date (YYYY-MM-DD)")
    args = parser.parse_args()

    cfg = load_config()
    offset = resolve_server_offset(cfg.get("market_data", {}))
    if offset <= 0:
        logger.error("resolved server_time_offset_hours=%s is not > 0; refusing to rebuild without an offset", offset)
        raise SystemExit(1)
    logger.info("Using server_time_offset_hours=%s", offset)

    def broker_symbol(internal: str) -> str:
        """Resolve internal asset name to the FxPro MT5 symbol (GOLD/SILVER/BITCOIN)."""
        asset = cfg.get("assets", {}).get(internal, {})
        return asset.get("mt5_symbol", internal)

    db_path = args.db_path
    conn = get_connection(db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ohlcv\\_%' ESCAPE '\\'"
    ).fetchall() if r[0].startswith("ohlcv_")]
    logger.info("Found ohlcv tables: %s", tables)

    tf_choices = sorted({t.replace("ohlcv_", "").upper() for t in tables})
    for tf in tf_choices:
        init_schema(db_path, [tf])
        table = f"ohlcv_{tf.lower()}"
        symbols = [r[0] for r in conn.execute(f"SELECT DISTINCT symbol FROM {table}").fetchall()]
        logger.info("[%s] symbols to rebuild: %s", tf, symbols)
        for symbol in symbols:
            start_s = args.start or COMBO_STARTS.get(tf, "2023-12-01")
            start = datetime.combine(datetime.strptime(start_s, "%Y-%m-%d").date(), time.min, tzinfo=timezone.utc)
            end = datetime.combine(datetime.strptime(args.end, "%Y-%m-%d").date(), time.max, tzinfo=timezone.utc)
            if end <= start:
                raise SystemExit("--end must be after --start")
            raw = fetch_candles_range(broker_symbol(symbol), tf, start, end, server_offset_hours=offset)
            stored = _to_storage_frame(raw, cfg.get("sessions", {}))
            if stored.empty:
                logger.error("[%s/%s] fetch returned empty; SKIPPING (old rows kept)", tf, symbol)
                continue
            # delete old broker-time rows for this symbol only after a good fetch
            conn.execute(f"DELETE FROM {table} WHERE symbol=?", (symbol,))
            conn.commit()
            upsert_candles(db_path, tf, symbol, stored)
            first = pd.to_datetime(stored["timestamp_utc"].iloc[0], unit="s", utc=True)
            last = pd.to_datetime(stored["timestamp_utc"].iloc[-1], unit="s", utc=True)
            logger.info("[%s/%s] rebuilt: %d rows (%s .. %s)", tf, symbol, len(stored),
                        first.isoformat(), last.isoformat())
    shutdown_mt5()
    logger.info("Done.")


if __name__ == "__main__":
    main()