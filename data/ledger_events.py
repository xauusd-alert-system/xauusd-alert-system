"""
Server-side append-only ledger for execution facts (``ledger_events``).

Receives ``ExecutionEvent`` facts from two producers — the Python sender
(``intent_created`` / ``request_result``) and the MQL5 observer
(``deal_added`` / ``order_history_added`` / ``position_modified`` /
``execution_reconciled`` / ``health_heartbeat``) — normalizes them into ONE
append-only table and serves the Execution Quality / Lifecycle Trace views.

Idempotency: ``event_id`` is the primary key and is deterministic on the
producer side, so outbox retries and restart reconciliation cannot duplicate
rows. UPDATE/DELETE are blocked by SQLite triggers, mirroring
``data/trading_event_ledger.py``.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import pandas as pd

from contracts.execution_contracts import ExecutionEvent
from data.intent_ledger import init_intent_ledger
from data.storage import get_connection

TABLE = "ledger_events"

COLUMNS = [
    "event_id",
    "schema_version",
    "source",
    "event_type",
    "intent_id",
    "asset_key",
    "broker_symbol",
    "magic_number",
    "account_mode",
    "precision",
    "order_ticket",
    "deal_ticket",
    "position_ticket",
    "deal_time_msc",
    "retcode",
    "requested_price",
    "fill_price",
    "filled_volume",
    "volume_requested",
    "spread_points",
    "commission",
    "swap",
    "latency_ms",
    "reason",
    "signature_valid",
    "received_at_utc_ms",
    "payload_json",
]


def init_ledger_events(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            event_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            intent_id TEXT,
            asset_key TEXT,
            broker_symbol TEXT NOT NULL,
            magic_number INTEGER,
            account_mode TEXT NOT NULL,
            precision TEXT NOT NULL,
            order_ticket INTEGER,
            deal_ticket INTEGER,
            position_ticket INTEGER,
            deal_time_msc INTEGER,
            retcode INTEGER,
            requested_price REAL,
            fill_price REAL,
            filled_volume REAL,
            volume_requested REAL,
            spread_points REAL,
            commission REAL,
            swap REAL,
            latency_ms INTEGER,
            reason TEXT,
            signature_valid INTEGER NOT NULL,
            received_at_utc_ms INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_source_time ON {TABLE}(source, received_at_utc_ms)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_intent ON {TABLE}(intent_id, received_at_utc_ms)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_asset_time ON {TABLE}(asset_key, received_at_utc_ms)")
        for action in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS prevent_{TABLE}_{action.lower()}
                BEFORE {action} ON {TABLE} BEGIN
                SELECT RAISE(ABORT, '{TABLE} is append-only'); END""")
        conn.commit()
    finally:
        conn.close()


def upsert_ledger_event(
    db_path: str,
    event: ExecutionEvent,
    *,
    signature_valid: bool,
    received_at_utc_ms: int | None = None,
) -> tuple[str, bool]:
    """Insert one fact; returns (event_id, inserted). Idempotent by event_id
    (a re-delivered fact returns inserted=False and changes nothing)."""
    init_ledger_events(db_path)
    received = int(received_at_utc_ms) if received_at_utc_ms is not None else int(event.received_at_utc_ms)
    payload_json = json.dumps(event.payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            f"""INSERT OR IGNORE INTO {TABLE} VALUES ({",".join("?" for _ in COLUMNS)})""",
            (
                event.event_id,
                event.schema_version,
                event.source,
                event.event_type,
                event.intent_id,
                event.asset_key,
                event.broker_symbol,
                event.magic_number,
                event.account_mode,
                event.precision,
                event.order_ticket,
                event.deal_ticket,
                event.position_ticket,
                event.deal_time_msc,
                event.retcode,
                event.requested_price,
                event.fill_price,
                event.filled_volume,
                event.volume_requested,
                event.spread_points,
                event.commission,
                event.swap,
                event.latency_ms,
                event.reason,
                1 if signature_valid else 0,
                received,
                payload_json,
            ),
        )
        conn.commit()
        return event.event_id, bool(cursor.rowcount == 1)
    finally:
        conn.close()


def read_ledger_events(
    db_path: str,
    *,
    source: str | None = None,
    event_type: str | None = None,
    asset_key: str | None = None,
    intent_id: str | None = None,
    since_ms: int | None = None,
    limit: int = 500,
) -> pd.DataFrame:
    init_ledger_events(db_path)
    query = f"SELECT * FROM {TABLE}"
    params: list[Any] = []
    clauses = []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if asset_key:
        clauses.append("asset_key = ?")
        params.append(asset_key)
    if intent_id:
        clauses.append("intent_id = ?")
        params.append(intent_id)
    if since_ms is not None:
        clauses.append("received_at_utc_ms >= ?")
        params.append(int(since_ms))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY received_at_utc_ms, event_id LIMIT ?"
    params.append(int(limit))
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def latest_ledger_activity_ms(db_path: str) -> int | None:
    """Max received_at over all facts; None when the ledger is empty."""
    init_ledger_events(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(f"SELECT MAX(received_at_utc_ms) FROM {TABLE}").fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


def execution_quality_summary(
    db_path: str,
    *,
    asset_key: str | None = None,
    since_ms: int | None = None,
    stale_after_ms: int = 6 * 3600 * 1000,
) -> dict[str, Any]:
    """Empirical execution-cost summary by precision bucket (plan Wave 3).

    Never mixes probe and passive observations in one statistic series:
    every percentile is split by ``precision``. The result carries an explicit
    ``stale`` flag so a UI can show STALE/OFFLINE instead of old numbers.
    """
    df = read_ledger_events(db_path, asset_key=asset_key, since_ms=since_ms, limit=100_000)
    if df.empty:
        return {
            "available": False,
            "source": "ledger_events",
            "mode": "demo",
            "as_of_utc_ms": None,
            "stale": True,
            "events": 0,
        }

    def _percentiles(series: pd.Series) -> dict[str, float | None]:
        valid = pd.to_numeric(series, errors="coerce").dropna()
        if valid.empty:
            return {f"p{int(q * 100):02d}": None for q in (0.5, 0.9, 0.95, 0.99)}
        return {f"p{int(q * 100):02d}": float(valid.quantile(q)) for q in (0.5, 0.9, 0.95, 0.99)}

    precision_groups = {}
    for precision, group in df.groupby("precision", dropna=False):
        if not precision:
            continue
        precision_groups[precision] = {
            "events": int(len(group)),
            "spread_points": _percentiles(group["spread_points"]),
            "latency_ms": _percentiles(group["latency_ms"]),
        }
        slippage = group["fill_price"] - group["requested_price"]
        slippage = slippage[pd.to_numeric(slippage, errors="coerce").notna()]
        # Sign convention: positive = adverse for the side of the deal.
        precision_groups[precision]["adverse_slippage_price_units"] = _percentiles(slippage)

    latest = latest_ledger_activity_ms(db_path)
    stale = latest is None or (time.time_ns() // 1_000_000 - latest) > stale_after_ms
    return {
        "available": True,
        "source": "ledger_events",
        "mode": "demo",  # observer is demo-only by construction; real mode is blocked
        "as_of_utc_ms": latest,
        "stale": bool(stale),
        "events": int(len(df)),
        "by_precision": precision_groups,
    }


def lifecycle_trace(db_path: str, intent_id: str) -> dict[str, Any]:
    """intent -> preflight -> request_result -> broker transactions -> reconciliation.

    Returns the ordered fact chain for one intent plus the linked intent row
    when one exists (``ledger_intents``).
    """
    init_ledger_events(db_path)
    init_intent_ledger(db_path)
    facts = read_ledger_events(db_path, intent_id=intent_id, limit=1000)
    if facts.empty:
        return {"intent_id": intent_id, "available": False, "facts": []}
    conn = get_connection(db_path)
    try:
        intent_row = conn.execute("SELECT * FROM ledger_intents WHERE intent_id = ?", (intent_id,)).fetchone()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(ledger_intents)").fetchall()]
    finally:
        conn.close()
    intent = {k: _clean_json(v) for k, v in zip(columns, intent_row)} if intent_row else None
    facts = [{k: _clean_json(v) for k, v in row.items()} for row in facts.to_dict("records")]
    return {
        "intent_id": intent_id,
        "available": True,
        "intent": intent,
        "facts": facts,
    }


def _clean_json(value: Any) -> Any:
    """SQLite NULL / pandas NaN / inf -> JSON-safe None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value
