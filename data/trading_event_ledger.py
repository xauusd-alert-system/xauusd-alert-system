"""Immutable source-of-truth ledger linking signal, decision, broker and PnL."""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import pandas as pd

from data.storage import get_connection

TABLE = "trading_events"
EVENT_TYPES = {
    "signal_created",
    "signal_armed",
    "signal_confirmed",
    "signal_rejected",
    "signal_expired",
    "signal_published",
    "order_submitted",
    "order_filled",
    "order_rejected",
    "stop_move_requested",
    "stop_move_confirmed",
    "stop_move_rejected",
    "partial_close_submitted",
    "partial_filled",
    "partial_rejected",
    "position_closed",
    # Wave-0 MQL5 plan: immutable SignalIntent recorded before order_send.
    "intent_created",
    # TradeGroupSpec v1 lifecycle (ТЗ §26): every event carries groupId/legId,
    # source, mode, broker ids, requested vs actual values, reason, retcode.
    "signal_validated",
    "trade_intent_created",
    "group_submitted",
    "group_rejected",
    "leg_submitted",
    "leg_filled",
    "tp1_filled",
    "be_requested",
    "be_retry",
    "be_confirmed",
    "tp2_filled",
    "tp3_filled",
    "stop_filled",
    "leg_rejected",
    "group_reconciled",
    # P1.5 demo MT5 execution (ТЗ §37): partial fills, group opened, orphan
    # broker positions and execution errors are explicit ledger facts.
    "leg_partially_filled",
    "group_opened",
    "orphan_broker_position",
    "execution_error",
    # P1.5.1 partial-submission compensation lifecycle (ТЗ P1.5.1 §20):
    # every compensation step is an explicit, idempotent ledger fact.
    "partial_submission",
    "compensation_requested",
    "compensation_confirmed",
    "compensation_failed",
    "failed_with_open_risk",
    # ТЗ 6.4 / P2-6: graceful shutdown marker (final poll done, state persisted).
    "system_shutdown",
}


def init_trading_event_ledger(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
            event_type TEXT NOT NULL, event_timestamp_utc INTEGER NOT NULL,
            recorded_at_ms INTEGER NOT NULL, signal_id TEXT NOT NULL,
            position_ticket INTEGER, order_ticket INTEGER, asset_key TEXT NOT NULL,
            strategy_version TEXT NOT NULL, config_hash TEXT NOT NULL,
            model_hash TEXT, feature_snapshot_hash TEXT, actor TEXT NOT NULL,
            reason TEXT, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
            previous_event_hash TEXT, event_hash TEXT NOT NULL UNIQUE,
            group_id TEXT, leg_id TEXT,
            source TEXT, source_type TEXT, source_id TEXT, observed_at_utc_ms INTEGER
        )""")
        # In-place migration: TradeGroupSpec v1 columns, then P1.6 provenance
        # columns (source vs actor separation, §26/§27).
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
        for column in ("group_id", "leg_id", "source", "source_type", "source_id", "observed_at_utc_ms"):
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE {TABLE} ADD COLUMN {column} TEXT"
                    if column != "observed_at_utc_ms"
                    else f"ALTER TABLE {TABLE} ADD COLUMN {column} INTEGER"
                )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_signal ON {TABLE}(signal_id, sequence)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_position ON {TABLE}(position_ticket, sequence)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_group ON {TABLE}(group_id, sequence)")
        for action in ("UPDATE", "DELETE"):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS prevent_{TABLE}_{action.lower()}
                BEFORE {action} ON {TABLE} BEGIN
                SELECT RAISE(ABORT, '{TABLE} is append-only'); END""")
        conn.commit()
    finally:
        conn.close()


def append_trading_event(
    db_path: str,
    *,
    event_type: str,
    signal_id: str,
    asset_key: str,
    strategy_version: str,
    config_hash: str,
    actor: str,
    event_timestamp_utc: int | None = None,
    model_hash: str | None = None,
    feature_snapshot_hash: str | None = None,
    position_ticket: int | None = None,
    order_ticket: int | None = None,
    reason: str | None = None,
    payload: dict | None = None,
    event_id: str | None = None,
    group_id: str | None = None,
    leg_id: str | None = None,
    source: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    observed_at_utc_ms: int | None = None,
) -> str:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported trading event: {event_type}")
    init_trading_event_ledger(db_path)
    payload_json = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    recorded = time.time_ns() // 1_000_000
    event_ts = int(event_timestamp_utc or recorded // 1000)
    eid = event_id or str(uuid.uuid4())
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(f"SELECT 1 FROM {TABLE} WHERE event_id=?", (eid,)).fetchone():
            conn.commit()
            return eid
        prior = conn.execute(f"SELECT sequence,event_hash FROM {TABLE} ORDER BY sequence DESC LIMIT 1").fetchone()
        sequence = int(prior[0]) + 1 if prior else 1
        previous = prior[1] if prior else None
        material = json.dumps(
            {
                "event_id": eid,
                "sequence": sequence,
                "event_type": event_type,
                "timestamp": event_ts,
                "signal_id": signal_id,
                "asset": asset_key,
                "strategy": strategy_version,
                "config": config_hash,
                "model": model_hash,
                "feature": feature_snapshot_hash,
                "actor": actor,
                "reason": reason,
                "payload_hash": payload_hash,
                "group_id": group_id,
                "leg_id": leg_id,
                "source": source,
                "source_type": source_type,
                "source_id": source_id,
                "observed_at_utc_ms": observed_at_utc_ms,
                "previous": previous,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        event_hash = hashlib.sha256(material.encode()).hexdigest()
        conn.execute(
            f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                sequence,
                event_type,
                event_ts,
                recorded,
                signal_id,
                position_ticket,
                order_ticket,
                asset_key,
                strategy_version,
                config_hash,
                model_hash,
                feature_snapshot_hash,
                actor,
                reason,
                payload_json,
                payload_hash,
                previous,
                event_hash,
                group_id,
                leg_id,
                source,
                source_type,
                source_id,
                observed_at_utc_ms,
            ),
        )
        conn.commit()
        return eid
    finally:
        conn.close()


def read_trading_events(db_path: str, signal_id: str | None = None) -> pd.DataFrame:
    init_trading_event_ledger(db_path)
    query = f"SELECT * FROM {TABLE}"
    params = []
    if signal_id:
        query += " WHERE signal_id=?"
        params.append(signal_id)
    query += " ORDER BY sequence"
    conn = get_connection(db_path)
    try:
        return pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()


def closed_position_pnls(db_path: str) -> list[float]:
    events = read_trading_events(db_path)
    out = []
    for row in events.loc[events["event_type"] == "position_closed"].to_dict("records"):
        payload = json.loads(row["payload_json"])
        if payload.get("realized_pnl") is not None:
            out.append(float(payload["realized_pnl"]))
    return out


def verify_event_chain(db_path: str) -> bool:
    df = read_trading_events(db_path)
    previous = None
    for row in df.to_dict("records"):
        nullable = lambda value: None if pd.isna(value) else value
        row_previous = nullable(row["previous_event_hash"])
        if row_previous != previous:
            return False
        material = json.dumps(
            {
                "event_id": row["event_id"],
                "sequence": int(row["sequence"]),
                "event_type": row["event_type"],
                "timestamp": int(row["event_timestamp_utc"]),
                "signal_id": row["signal_id"],
                "asset": row["asset_key"],
                "strategy": row["strategy_version"],
                "config": row["config_hash"],
                "model": nullable(row["model_hash"]),
                "feature": nullable(row["feature_snapshot_hash"]),
                "actor": row["actor"],
                "reason": nullable(row["reason"]),
                "payload_hash": row["payload_hash"],
                "group_id": nullable(row.get("group_id")),
                "leg_id": nullable(row.get("leg_id")),
                "source": nullable(row.get("source")),
                "source_type": nullable(row.get("source_type")),
                "source_id": nullable(row.get("source_id")),
                "observed_at_utc_ms": nullable(row.get("observed_at_utc_ms")),
                "previous": previous,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(material.encode()).hexdigest()
        if (
            expected != row["event_hash"]
            or hashlib.sha256(row["payload_json"].encode()).hexdigest() != row["payload_hash"]
        ):
            return False
        previous = row["event_hash"]
    return True
