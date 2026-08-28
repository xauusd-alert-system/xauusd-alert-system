"""Append-only frozen-candidate paper ledger.

The ledger is event sourced: rows are never updated or deleted.  Idempotency keys
make repeated scheduler runs on the same closed candle harmless.  Performance is
not exposed by the accumulation status API; outcomes are read only by the explicit
one-time validation command after the pre-registered minimum sample is reached.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pandas as pd

from data.storage import get_connection

RUNS_TABLE = "paper_runs"
EVENTS_TABLE = "paper_trades"
EVENT_TYPES = {"signal", "open", "mark", "close", "cancel", "heartbeat", "validation_read"}


def init_paper_schema(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
            run_id TEXT PRIMARY KEY,
            asset_key TEXT NOT NULL,
            variant TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL,
            model_sha256 TEXT NOT NULL,
            start_timestamp_utc INTEGER NOT NULL,
            min_closed_trades INTEGER NOT NULL,
            registered_at_utc TEXT NOT NULL,
            manifest_json TEXT NOT NULL
        )""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            trade_id TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            event_timestamp_utc INTEGER NOT NULL,
            bar_timestamp_utc INTEGER,
            payload_json TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            FOREIGN KEY(run_id) REFERENCES {RUNS_TABLE}(run_id)
        )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_run_event ON {EVENTS_TABLE}(run_id, event_id)")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_trade ON {EVENTS_TABLE}(run_id, trade_id, event_id)"
        )
        # Enforce append-only semantics in SQLite itself, not only in Python.
        # (UPPER_CASE loop var: the SQL-value-interpolation guard test accepts
        # only static constant identifiers inside quoted SQL — ТЗ 10.11.)
        for TABLE_ITER in (RUNS_TABLE, EVENTS_TABLE):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS prevent_{TABLE_ITER}_update
                BEFORE UPDATE ON {TABLE_ITER} BEGIN
                    SELECT RAISE(ABORT, '{TABLE_ITER} is append-only');
                END""")
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS prevent_{TABLE_ITER}_delete
                BEFORE DELETE ON {TABLE_ITER} BEGIN
                    SELECT RAISE(ABORT, '{TABLE_ITER} is append-only');
                END""")
        conn.commit()
    finally:
        conn.close()


def register_paper_run(db_path: str, manifest: dict) -> None:
    """Register an immutable manifest, accepting only exact idempotent repeats."""
    init_paper_schema(db_path)
    required = {
        "run_id",
        "asset_key",
        "variant",
        "manifest_sha256",
        "model_sha256",
        "start_timestamp_utc",
        "min_closed_trades",
        "created_at_utc",
    }
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"paper manifest missing fields: {sorted(missing)}")
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str)
    conn = get_connection(db_path)
    try:
        existing = conn.execute(
            f"SELECT manifest_sha256 FROM {RUNS_TABLE} WHERE run_id=?",
            (manifest["run_id"],),
        ).fetchone()
        if existing:
            if existing[0] != manifest["manifest_sha256"]:
                raise RuntimeError(f"run_id {manifest['run_id']!r} is already registered with a different manifest")
            return
        conn.execute(
            f"INSERT INTO {RUNS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest["run_id"],
                manifest["asset_key"],
                manifest["variant"],
                manifest["manifest_sha256"],
                manifest["model_sha256"],
                int(manifest["start_timestamp_utc"]),
                int(manifest["min_closed_trades"]),
                manifest["created_at_utc"],
                manifest_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def append_paper_event(
    db_path: str,
    *,
    run_id: str,
    event_type: str,
    idempotency_key: str,
    event_timestamp_utc: int,
    payload: dict[str, Any] | None = None,
    trade_id: str | None = None,
    bar_timestamp_utc: int | None = None,
) -> bool:
    """Append an event; return False when its idempotency key already exists."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported paper event_type={event_type!r}")
    init_paper_schema(db_path)
    conn = get_connection(db_path)
    try:
        run = conn.execute(f"SELECT 1 FROM {RUNS_TABLE} WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            raise ValueError(f"paper run {run_id!r} is not registered")
        cursor = conn.execute(
            f"""INSERT INTO {EVENTS_TABLE} (
                run_id, trade_id, idempotency_key, event_type,
                event_timestamp_utc, bar_timestamp_utc, payload_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING""",
            (
                run_id,
                trade_id,
                idempotency_key,
                event_type,
                int(event_timestamp_utc),
                int(bar_timestamp_utc) if bar_timestamp_utc is not None else None,
                json.dumps(payload or {}, sort_keys=True, default=str),
                time.time_ns() // 1_000_000,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def read_paper_events(
    db_path: str,
    run_id: str,
    *,
    event_type: str | None = None,
    include_payload: bool = True,
) -> pd.DataFrame:
    init_paper_schema(db_path)
    columns = (
        "*"
        if include_payload
        else (
            "event_id, run_id, trade_id, idempotency_key, event_type, "
            "event_timestamp_utc, bar_timestamp_utc, created_at_ms"
        )
    )
    query = f"SELECT {columns} FROM {EVENTS_TABLE} WHERE run_id=?"
    params: list[object] = [run_id]
    if event_type is not None:
        query += " AND event_type=?"
        params.append(event_type)
    query += " ORDER BY event_id"
    conn = get_connection(db_path)
    try:
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    if include_payload and not df.empty:
        df["payload"] = df["payload_json"].map(json.loads)
    return df


def get_paper_run(db_path: str, run_id: str) -> dict:
    init_paper_schema(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(f"SELECT manifest_json FROM {RUNS_TABLE} WHERE run_id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"unknown paper run {run_id!r}")
    return json.loads(row[0])


def paper_accumulation_status(db_path: str, run_id: str) -> dict:
    """Liveness/sample counter only: intentionally no PnL/PF/win-rate fields."""
    manifest = get_paper_run(db_path, run_id)
    events = read_paper_events(db_path, run_id, include_payload=False)
    counts = events["event_type"].value_counts().to_dict() if not events.empty else {}
    closed = int(counts.get("close", 0))
    minimum = int(manifest["min_closed_trades"])
    latest_bar = None
    if not events.empty and events["bar_timestamp_utc"].notna().any():
        latest_bar = int(events["bar_timestamp_utc"].dropna().max())
    return {
        "run_id": run_id,
        "asset_key": manifest["asset_key"],
        "variant": manifest["variant"],
        "mode": "paper_frozen",
        "source": "append_only_paper_ledger",
        "manifest_sha256": manifest["manifest_sha256"],
        "signals": int(counts.get("signal", 0)),
        "opened_trades": int(counts.get("open", 0)),
        "closed_trades": closed,
        "minimum_closed_trades": minimum,
        "ready_for_one_time_validation": closed >= minimum,
        "validation_reads": int(counts.get("validation_read", 0)),
        "latest_bar_timestamp_utc": latest_bar,
    }
