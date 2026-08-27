"""import_external_candles - load external XAUUSD history into the candles store.

Unblocks real-data experiments in environments where the MT5 terminal is not
reachable (TZ_BOOKS report: "experiments on real data" was blocked by the
empty market-data sandbox). The primary tested source is the public
Kaggle/MT4 export mirrored at github.com/BaseMax/XAUUSD-LSTM
(``XAU_15m_data.csv``, 480k M15 bars 2004-2025), but any MT4-style
``Date;Open;High;Low;Close;Volume`` CSV works.

Target: ``data/market_data_external.sqlite`` (git-ignored, like all *.sqlite)
with the ``candles`` schema the loaders already read:

    candles(symbol TEXT, timeframe TEXT, time INTEGER, open REAL, high REAL,
            low REAL, close REAL, volume REAL, UNIQUE(symbol, timeframe, time))

Honesty rules:
* rows failing OHLC invariants are REJECTED (counted, never silently fixed);
* re-import is idempotent (INSERT OR REPLACE on the natural key);
* provenance (source URL, commit, row counts, date range) is recorded in
  ``source_meta`` so any artifact built from this data can cite it;
* the importer never touches ``data/market_data_mt5.sqlite`` - terminal data
  and external data stay in separate files.

Usage::

    python -m scripts.import_external_candles \
        --csv /tmp/XAUUSD-LSTM/XAU_15m_data.csv \
        --symbol XAUUSD --timeframe M15 \
        --db data/market_data_external.sqlite
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logger = logging.getLogger("import_external_candles")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    time      INTEGER NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL,
    UNIQUE(symbol, timeframe, time)
);
CREATE INDEX IF NOT EXISTS idx_candles_sym_tf_time
    ON candles (symbol, timeframe, time);
CREATE TABLE IF NOT EXISTS source_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# MT4 export date formats seen in the wild, most specific first
_DATE_FORMATS = ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M",
                 "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                 "%Y/%m/%d %H:%M")


def parse_mt4_timestamp(value: str) -> int | None:
    """Epoch seconds (UTC) from an MT4-style ``2004.06.11 07:15`` string."""
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return int(datetime.strptime(text, fmt)
                       .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    try:  # pandas fallback (ISO etc.)
        ts = pd.to_datetime(text)
        if ts is not pd.NaT:
            return int(ts.tz_localize("UTC").timestamp()
                       if ts.tzinfo is None else ts.timestamp())
    except Exception:
        pass
    return None


def read_ohlcv_csv(path: str) -> pd.DataFrame:
    """Read an MT4-style OHLCV CSV (semicolon or comma separated)."""
    for sep in (";", ","):
        df = pd.read_csv(path, sep=sep, encoding="utf-8-sig")
        cols = {c.strip().lower(): c for c in df.columns}
        required = {"date", "open", "high", "low", "close"}
        if required.issubset(cols):
            volume_col = cols.get("volume") or cols.get("vol") or cols.get("tickvol")
            out = pd.DataFrame({
                "time": df[cols["date"]].map(parse_mt4_timestamp),
                "open": pd.to_numeric(df[cols["open"]], errors="coerce"),
                "high": pd.to_numeric(df[cols["high"]], errors="coerce"),
                "low": pd.to_numeric(df[cols["low"]], errors="coerce"),
                "close": pd.to_numeric(df[cols["close"]], errors="coerce"),
                "volume": (pd.to_numeric(df[volume_col], errors="coerce")
                           if volume_col else 0.0),
            })
            return out.dropna(subset=["time", "open", "high", "low", "close"])
    raise ValueError(f"{path}: no Date/Open/High/Low/Close header found")


def validate_ohlcv(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop bars violating OHLC invariants; return (clean, rejected_count)."""
    ok = (
        (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-9)
        & (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-9)
        & (df[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (df["high"] >= df["low"])
    )
    rejected = int((~ok).sum())
    return df[ok].copy(), rejected


def import_csv(csv_path: str, db_path: str, symbol: str, timeframe: str,
               max_bars: int | None = None,
               source_url: str | None = None) -> dict:
    """Import an OHLCV CSV into the candles table (idempotent)."""
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(csv_path)

    raw = read_ohlcv_csv(csv_path)
    clean, rejected = validate_ohlcv(raw)
    clean = clean.drop_duplicates(subset=["time"]).sort_values("time")
    if max_bars and len(clean) > max_bars:
        clean = clean.tail(int(max_bars)).copy()   # most recent regime
    if len(clean) == 0:
        raise ValueError("no valid bars after validation")

    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        con.executemany(
            "INSERT OR REPLACE INTO candles "
            "(symbol, timeframe, time, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(symbol, timeframe, int(r.time), float(r.open), float(r.high),
              float(r.low), float(r.close),
              float(r.volume) if pd.notna(r.volume) else None)
             for r in clean.itertuples()])
        meta = {
            "source": source_url or os.path.abspath(csv_path),
            "imported_at_utc": datetime.now(timezone.utc)
                                        .isoformat(timespec="seconds"),
            "symbol": symbol,
            "timeframe": timeframe,
            "rows_csv": int(len(raw)),
            "rows_rejected": rejected,
            "rows_imported": int(len(clean)),
            "first_bar_utc": datetime.fromtimestamp(
                int(clean["time"].iloc[0]), tz=timezone.utc).isoformat(),
            "last_bar_utc": datetime.fromtimestamp(
                int(clean["time"].iloc[-1]), tz=timezone.utc).isoformat(),
        }
        con.executemany("INSERT OR REPLACE INTO source_meta (key, value) "
                        "VALUES (?, ?)", [(k, str(v)) for k, v in meta.items()])
        con.commit()
    finally:
        con.close()
    logger.info("imported %d %s %s bars (%s .. %s), %d rejected",
                len(clean), symbol, timeframe, meta["first_bar_utc"],
                meta["last_bar_utc"], rejected)
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", required=True)
    parser.add_argument("--db", default="data/market_data_external.sqlite")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--max-bars", type=int, default=None,
                        help="keep only the most recent N bars")
    parser.add_argument("--source-url", default=None,
                        help="provenance: where the CSV came from")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    meta = import_csv(args.csv, args.db, args.symbol, args.timeframe,
                      args.max_bars, args.source_url)
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
