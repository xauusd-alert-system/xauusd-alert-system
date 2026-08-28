"""
SQLite persistence layer for generated signals (audit trail of every signal the
system produced, whether or not it triggered a Telegram alert).

Design mirrors data/storage.py's pattern: single table, UTC epoch timestamps,
idempotent upserts keyed on timestamp_utc so re-running the scheduler for the
same candle never creates duplicate rows.

This is critical for accountability: the project brief requires transparency into
WHY a signal was or wasn't sent, not just the alerts that went out. Every row here
answers "what did the system think at time T", regardless of alert_sent status.
"""
import json
import os
import sqlite3

import pandas as pd

TABLE_NAME = "signal_log"


def get_connection(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_schema(db_path: str):
    conn = get_connection(db_path)
    try:
        # Migrate a legacy single-symbol table (PK = timestamp_utc, no symbol column):
        # rename it once so a fresh multi-symbol table can be created. The old data is
        # preserved under {TABLE_NAME}_legacy_single_symbol rather than silently dropped.
        columns = conn.execute(f"PRAGMA table_info({TABLE_NAME});").fetchall()
        if columns:
            names = {row[1] for row in columns}
            pk_cols = [c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5] > 0]
            is_legacy = "symbol" not in names or pk_cols != ["symbol", "timestamp_utc"]
            if is_legacy:
                legacy = f"{TABLE_NAME}_legacy_single_symbol"
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (legacy,)
                ).fetchone()
                if exists:
                    raise RuntimeError(
                        f"Legacy backup table {legacy!r} already exists. "
                        f"Move or delete it deliberately before migrating {TABLE_NAME!r}."
                    )
                conn.execute(f"ALTER TABLE {TABLE_NAME} RENAME TO {legacy};")

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                symbol TEXT NOT NULL,
                timestamp_utc INTEGER NOT NULL,
                generated_at TEXT NOT NULL,
                bias TEXT NOT NULL,
                confidence REAL NOT NULL,
                regime TEXT NOT NULL,
                session TEXT NOT NULL,
                entry_zone TEXT,
                invalidation REAL,
                targets TEXT,
                reasoning_summary TEXT,
                alert_sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, timestamp_utc)
            );
        """)
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
        optional = {
            "signal_id": "TEXT", "signal_state": "TEXT", "strategy_version": "TEXT",
            "config_hash": "TEXT", "model_hash": "TEXT", "feature_snapshot_hash": "TEXT",
            "expires_at_utc": "INTEGER", "published_at_utc": "INTEGER",
            "publish_latency_seconds": "INTEGER",
        }
        for name, sql_type in optional.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {name} {sql_type}")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_sym_ts ON {TABLE_NAME}(symbol, timestamp_utc);")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_ts ON {TABLE_NAME}(timestamp_utc);")
        conn.commit()
    finally:
        conn.close()


def log_signal(db_path: str, signal: dict, alert_sent: bool, symbol: str = "XAUUSD"):
    """
    Persists one signal dict (the exact JSON shape returned by
    realtime/pipeline.py::generate_signal) plus whether an alert was actually sent.
    Uses INSERT OR REPLACE keyed on (symbol, timestamp_utc) so re-scoring the same
    candle for the same asset is idempotent, while multiple assets at the same
    moment never overwrite each other (multi-asset audit trail).
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE_NAME}
                (symbol, timestamp_utc, generated_at, bias, confidence, regime, session,
                 entry_zone, invalidation, targets, reasoning_summary, alert_sent,
                 signal_id, signal_state, strategy_version, config_hash, model_hash,
                 feature_snapshot_hash, expires_at_utc, published_at_utc, publish_latency_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol,
                signal["timestamp_utc"],
                signal["generated_at"],
                signal["bias"],
                signal["confidence"],
                signal["regime"],
                signal["session"],
                json.dumps(signal["entry_zone"]) if signal.get("entry_zone") else None,
                signal.get("invalidation"),
                json.dumps(signal["targets"]) if signal.get("targets") else None,
                signal.get("reasoning_summary", ""),
                int(alert_sent),
                signal.get("signal_id"), signal.get("signal_state"),
                signal.get("strategy_version"), signal.get("config_hash"),
                signal.get("model_hash"), signal.get("feature_snapshot_hash"),
                signal.get("expires_at_utc"), signal.get("published_at_utc"),
                signal.get("publish_latency_seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def read_signal_history(
    db_path: str,
    start_ts: int = None,
    end_ts: int = None,
    symbol: str = None,
) -> pd.DataFrame:
    """Read logged signals for auditing/reporting, sorted ascending by timestamp."""
    conn = get_connection(db_path)
    try:
        query = f"SELECT * FROM {TABLE_NAME}"
        clauses, params = [], []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if start_ts is not None:
            clauses.append("timestamp_utc >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("timestamp_utc <= ?")
            params.append(end_ts)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY symbol ASC, timestamp_utc ASC"
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    return df
