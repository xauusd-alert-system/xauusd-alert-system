"""
SQLite storage for multi-asset OHLCV candles.

Each timeframe has its own table. Within each table, candles are uniquely
identified by (symbol, timestamp_utc), allowing multiple assets to share an
M15 table safely.
"""
import os
import sqlite3

import pandas as pd

REQUIRED_COLUMNS = [
    "timestamp_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "session",
]


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection and create the parent directory if necessary."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _table_name(timeframe: str) -> str:
    normalized = timeframe.lower()
    if normalized not in {"m1", "m5", "m15", "h1", "h4"}:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    return f"ohlcv_{normalized}"


def _create_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            symbol TEXT NOT NULL,
            timestamp_utc INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            session TEXT NOT NULL,
            PRIMARY KEY (symbol, timestamp_utc)
        );
    """)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_symbol_ts "
        f"ON {table}(symbol, timestamp_utc);"
    )


def _has_legacy_schema(conn: sqlite3.Connection, table: str) -> bool:
    columns = conn.execute(f"PRAGMA table_info({table});").fetchall()
    if not columns:
        return False

    names = {column[1] for column in columns}
    primary_key_columns = [
        column[1]
        for column in sorted(columns, key=lambda column: column[5])
        if column[5] > 0
    ]
    return "symbol" not in names or primary_key_columns != ["symbol", "timestamp_utc"]


def init_schema(db_path: str, timeframes: list[str]) -> None:
    """
    Create symbol-aware tables.

    If a legacy single-symbol table exists, rename it once as a backup rather
    than silently mixing its old data with six new symbols.
    """
    conn = get_connection(db_path)
    try:
        for timeframe in timeframes:
            table = _table_name(timeframe)

            if _has_legacy_schema(conn, table):
                legacy_table = f"{table}_legacy_single_symbol"
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (legacy_table,),
                ).fetchone()

                if exists:
                    raise RuntimeError(
                        f"Legacy backup table {legacy_table!r} already exists. "
                        f"Move or delete it deliberately before migrating {table!r}."
                    )

                conn.execute(f"ALTER TABLE {table} RENAME TO {legacy_table}")

            _create_table(conn, table)

        conn.commit()
    finally:
        conn.close()


def upsert_candles(
    db_path: str,
    timeframe: str,
    symbol: str,
    df: pd.DataFrame,
) -> None:
    """Insert or replace one asset's candles for a timeframe."""
    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {sorted(missing)}")

    if df.empty:
        return

    table = _table_name(timeframe)
    init_schema(db_path, [timeframe])

    rows = [
        (
            symbol,
            int(row.timestamp_utc),
            float(row.open),
            float(row.high),
            float(row.low),
            float(row.close),
            float(row.volume),
            str(row.session),
        )
        for row in df[REQUIRED_COLUMNS].itertuples(index=False)
    ]

    conn = get_connection(db_path)
    try:
        conn.executemany(
            f"""
            INSERT INTO {table}
                (symbol, timestamp_utc, open, high, low, close, volume, session)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timestamp_utc) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                session=excluded.session
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def read_candles(
    db_path: str,
    timeframe: str,
    symbol: str,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> pd.DataFrame:
    """Read one asset's candles, ascending by UTC epoch timestamp."""
    table = _table_name(timeframe)
    init_schema(db_path, [timeframe])

    query = f"SELECT * FROM {table} WHERE symbol = ?"
    params: list[object] = [symbol]

    if start_ts is not None:
        query += " AND timestamp_utc >= ?"
        params.append(int(start_ts))

    if end_ts is not None:
        query += " AND timestamp_utc <= ?"
        params.append(int(end_ts))

    query += " ORDER BY timestamp_utc ASC"

    conn = get_connection(db_path)
    try:
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
