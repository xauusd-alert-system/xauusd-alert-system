"""
Immutable local store of ``SignalIntent`` rows (``ledger_intents``).

The Python sender persists an intent **before** ``order_send`` (plan Wave-0
contract: intent is created first, then the broker request carries a
correlation-safe short id in the order comment). The table is append-only and
idempotent by ``intent_id``; the same intent delivered twice (restart, retry)
never creates a second row.
"""

from __future__ import annotations

import json
import time

from contracts.execution_contracts import SignalIntent

TABLE = "ledger_intents"


def init_intent_ledger(db_path: str) -> None:
    from data.storage import get_connection

    conn = get_connection(db_path)
    try:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            intent_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            asset_key TEXT NOT NULL,
            broker_symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            requested_volume REAL NOT NULL,
            entry_price REAL NOT NULL,
            sl_price REAL,
            tp_price REAL,
            model_version TEXT,
            feature_manifest_hash TEXT,
            config_hash TEXT,
            mode TEXT NOT NULL,
            magic_number INTEGER NOT NULL,
            source TEXT NOT NULL,
            signal_id TEXT,
            created_at_utc_ms INTEGER NOT NULL,
            intent_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_asset ON {TABLE}(asset_key, created_at_utc_ms)")
        for action in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS prevent_{TABLE}_{action.lower()}
                BEFORE {action} ON {TABLE} BEGIN
                SELECT RAISE(ABORT, '{TABLE} is append-only'); END""")
        conn.commit()
    finally:
        conn.close()


def append_signal_intent(db_path: str, intent: SignalIntent) -> str:
    """Persist an intent idempotently; returns its intent_id."""
    init_intent_ledger(db_path)
    payload_json = json.dumps(intent.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), default=str)
    from data.storage import get_connection

    conn = get_connection(db_path)
    try:
        conn.execute(
            f"""INSERT OR IGNORE INTO {TABLE} VALUES ({",".join("?" for _ in range(19))})""",
            (
                intent.intent_id,
                intent.schema_version,
                intent.asset_key,
                intent.broker_symbol,
                intent.side,
                intent.requested_volume,
                intent.entry_price,
                intent.sl_price,
                intent.tp_price,
                intent.model_version,
                intent.feature_manifest_hash,
                intent.config_hash,
                intent.mode,
                intent.magic_number,
                intent.source,
                intent.signal_id,
                intent.created_at_utc_ms,
                intent.canonical_hash(),
                payload_json,
            ),
        )
        conn.commit()
        return intent.intent_id
    finally:
        conn.close()


def read_signal_intent(db_path: str, intent_id: str) -> dict | None:
    init_intent_ledger(db_path)
    from data.storage import get_connection

    conn = get_connection(db_path)
    try:
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE intent_id = ?", (intent_id,)).fetchone()
        if row is None:
            return None
        columns = [c[1] for c in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()]
        return dict(zip(columns, row))
    finally:
        conn.close()


def now_ms() -> int:
    return time.time_ns() // 1_000_000
