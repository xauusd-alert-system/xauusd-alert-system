"""
SQLite storage layer for OHLCV candles.
Design decision: a single table per timeframe (e.g. ohlcv_m1, ohlcv_m5, ...) keeps
queries simple and avoids a giant mixed-timeframe table with a timeframe column that
would need constant filtering. All timestamps are stored as UTC epoch seconds (INTEGER)
to avoid timezone ambiguity and to allow fast range queries via index.
"""
import sqlite3
import os
import pandas as pd

REQUIRED_COLUMNS = ["timestamp_utc", "open", "high", "low", "close", "volume", "session"]


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open (and create parent dir for) a SQLite connection."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")  # better concurrent read/write behavior
    return conn


def _table_name(timeframe: str) -> str:
    return f"ohlcv_{timeframe.lower()}"


def init_schema(db_path: str, timeframes: list):
    """Create one OHLCV table per timeframe if not exists, with a unique index on timestamp."""
    conn = get_connection(db_path)
    try:
        for tf in timeframes:
            tbl = _table_name(tf)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {tbl} (
                    timestamp_utc INTEGER PRIMARY KEY,  -- epoch seconds, UTC, unique -> prevents duplicate candles
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    session TEXT NOT NULL
                );
            """)
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{tbl}_ts ON {tbl}(timestamp_utc);")
        conn.commit()
    finally:
        conn.close()


def upsert_candles(db_path: str, timeframe: str, df: pd.DataFrame):
    """
    Insert or replace candles for a given timeframe.
    df must contain REQUIRED_COLUMNS. Using INSERT OR REPLACE keyed on timestamp_utc
    makes ingestion idempotent - re-pulling overlapping ranges is always safe.
    """
    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {missing}")

    tbl = _table_name(timeframe)
    conn = get_connection(db_path)
    try:
        rows = df[REQUIRED_COLUMNS].values.tolist()
        conn.executemany(
            f"INSERT OR REPLACE INTO {tbl} "
            f"(timestamp_utc, open, high, low, close, volume, session) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def read_candles(db_path: str, timeframe: str, start_ts: int = None, end_ts: int = None) -> pd.DataFrame:
    """
    Read candles for a timeframe within an optional UTC epoch-second range.
    Returns a DataFrame sorted ascending by timestamp - callers must NEVER
    assume future rows are visible at index i (see features/ no-lookahead tests).
    """
    tbl = _table_name(timeframe)
    conn = get_connection(db_path)
    try:
        query = f"SELECT * FROM {tbl}"
        clauses, params = [], []
        if start_ts is not None:
            clauses.append("timestamp_utc >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp_utc <= ?")
            params.append(end_ts)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp_utc ASC"
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    return df
