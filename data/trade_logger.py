"""
SQLite persistence for executed real-world trades, including features at signal time
and actual trade outcome (pnl, close_price, outcome label) for weekly ML retraining.
"""
import os
import sqlite3
import json
import pandas as pd

TABLE_NAME = "executed_trades"


def get_connection(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


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
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_symbol ON {TABLE_NAME}(symbol);")
        conn.commit()
    finally:
        conn.close()


def log_trade_entry(db_path: str, ticket: int, symbol: str, bias: str, entry_time: int, entry_price: float, features: dict):
    """
    Logs trade entry with feature values serialized to JSON.
    """
    init_trade_log_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE_NAME}
                (ticket, symbol, bias, entry_time, entry_price, close_time, close_price, pnl, outcome, features)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)""",
            (
                ticket,
                symbol,
                bias,
                entry_time,
                entry_price,
                json.dumps(features or {}),
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


def read_executed_trades(db_path: str, symbol: str = None) -> pd.DataFrame:
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
