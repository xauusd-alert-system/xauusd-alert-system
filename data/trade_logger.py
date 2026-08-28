"""
SQLite persistence for executed real-world trades, including features at signal time
and actual trade outcome (pnl, close_price, outcome label) for weekly ML retraining.
"""
import json
import os
import sqlite3
from typing import Optional

import pandas as pd

TABLE_NAME = "executed_trades"


def get_connection(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


# TradeGroupSpec v1 columns (ТЗ §27 data/trade_logger.py): nullable, added
# in-place so legacy executed_trades rows migrate non-destructively.
GROUP_COLUMNS = [
    "group_id", "intent_id", "leg_id", "profile_id", "schema_version",
    "requested_entry", "actual_fill", "tp1", "tp2", "tp3", "sl",
    "be_requested", "be_confirmed",
]


def init_trade_log_schema(db_path: str):
    conn = get_connection(db_path)
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                ticket INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                bias TEXT NOT NULL,
                entry_time INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                close_time INTEGER,
                close_price REAL,
                pnl REAL,
                outcome INTEGER,
                features TEXT NOT NULL
            );
        """)
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
        for column in GROUP_COLUMNS:
            if column not in existing:
                conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {column} TEXT")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_symbol ON {TABLE_NAME}(symbol);")
        conn.commit()
    finally:
        conn.close()


def log_trade_entry(db_path: str, ticket: int, symbol: str, bias: str, entry_time: int,
                    entry_price: float, features: dict, *,
                    group_id: str | None = None, intent_id: str | None = None,
                    leg_id: str | None = None, profile_id: str | None = None,
                    schema_version: str | None = None, requested_entry: float | None = None,
                    actual_fill: float | None = None, tp1: float | None = None,
                    tp2: float | None = None, tp3: float | None = None,
                    sl: float | None = None, be_requested: float | None = None,
                    be_confirmed: float | None = None):
    """
    Logs trade entry with feature values serialized to JSON.

    TradeGroupSpec v1 columns are optional; legacy callers keep working with
    NULL group fields (migration is non-destructive).
    """
    init_trade_log_schema(db_path)
    conn = get_connection(db_path)
    try:
        columns = (
            "ticket, symbol, bias, entry_time, entry_price, close_time, close_price, "
            "pnl, outcome, features, " + ", ".join(GROUP_COLUMNS)
        )
        placeholders = ", ".join("?" for _ in range(10 + len(GROUP_COLUMNS)))
        conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE_NAME}
                ({columns})
                VALUES ({placeholders})""",
            (
                ticket, symbol, bias, entry_time, entry_price,
                None, None, None, None,
                json.dumps(features or {}),
                group_id, intent_id, leg_id, profile_id, schema_version,
                requested_entry, actual_fill, tp1, tp2, tp3, sl,
                be_requested, be_confirmed,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def log_trade_close(db_path: str, ticket: int, close_time: int, close_price: float, pnl: float):
    """
    Updates the trade row with close details and outcome flag (1 if pnl >= 0 else 0).
    """
    init_trade_log_schema(db_path)
    conn = get_connection(db_path)
    outcome = 1 if pnl >= 0 else 0
    try:
        conn.execute(
            f"""UPDATE {TABLE_NAME}
                SET close_time = ?, close_price = ?, pnl = ?, outcome = ?
                WHERE ticket = ?""",
            (close_time, close_price, pnl, outcome, ticket),
        )
        conn.commit()
    finally:
        conn.close()


def read_executed_trades(db_path: str, symbol: Optional[str] = None) -> pd.DataFrame:
    """
    Reads executed trades dataframe for ML retraining.
    """
    init_trade_log_schema(db_path)
    conn = get_connection(db_path)
    try:
        query = f"SELECT * FROM {TABLE_NAME} WHERE outcome IS NOT NULL"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY entry_time ASC"
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    return df
