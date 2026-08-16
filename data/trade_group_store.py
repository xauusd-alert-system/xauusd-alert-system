"""
Durable store for TradeGroupSpec v1 lifecycle state (ТЗ §25).

All open groups survive process/server/Telegram restart and temporary MT5
disconnects. Persisted per group: spec (immutable geometry), state, leg states,
BE state, broker ids and the ``submitted`` flag.

``try_mark_submitted`` is the restart-safety guard: only the FIRST caller may
transition a group from not-submitted to submitted (an UPDATE with
``WHERE submitted=0`` that affects 0 rows returns False), so recovery after a
restart can never submit a duplicate order for an already-submitted group.
"""
from __future__ import annotations

import json
import time
from typing import Any

from data.storage import get_connection
from execution.trade_group import GroupState, TradeGroupSpec

TABLE = "trade_groups"


def init_trade_group_store(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            group_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            spec_hash TEXT NOT NULL,
            state TEXT NOT NULL,
            legs_json TEXT NOT NULL,
            be_json TEXT NOT NULL,
            broker_ids_json TEXT NOT NULL,
            submitted INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_state ON {TABLE}(state)")
        conn.commit()
    finally:
        conn.close()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def save_group(
    db_path: str,
    spec: TradeGroupSpec,
    *,
    state: GroupState = GroupState.DRAFT,
    legs: list[dict[str, Any]] | None = None,
    be_state: dict[str, Any] | None = None,
    broker_ids: dict[str, Any] | None = None,
    submitted: bool = False,
) -> None:
    """Insert or update one group (state is mutable; geometry is not)."""
    init_trade_group_store(db_path)
    now = _now_ms()
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE}
                (group_id, schema_version, spec_json, spec_hash, state, legs_json,
                 be_json, broker_ids_json, submitted, created_at_ms, updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                spec.group_id, spec.schema_version,
                spec.model_dump_json(), spec.geometry_hash(),
                state.value, json.dumps(legs or []),
                json.dumps(be_state or {}), json.dumps(broker_ids or {}),
                1 if submitted else 0, now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_group(db_path: str, group_id: str) -> dict[str, Any] | None:
    """Load one group; None when absent. ``spec`` is a TradeGroupSpec."""
    init_trade_group_store(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT * FROM {TABLE} WHERE group_id = ?", (group_id,)
        ).fetchone()
        if row is None:
            return None
        columns = [c[1] for c in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()]
        data = dict(zip(columns, row))
    finally:
        conn.close()
    data["spec"] = TradeGroupSpec.model_validate_json(data.pop("spec_json"))
    data["state"] = GroupState(data["state"])
    data["legs"] = json.loads(data.pop("legs_json") or "[]")
    data["be_state"] = json.loads(data.pop("be_json") or "{}")
    data["broker_ids"] = json.loads(data.pop("broker_ids_json") or "{}")
    data["submitted"] = bool(data["submitted"])
    return data


def list_groups(db_path: str, state: GroupState | None = None) -> list[dict[str, Any]]:
    """List groups, optionally filtered by state (spec parsed)."""
    init_trade_group_store(db_path)
    conn = get_connection(db_path)
    try:
        query = f"SELECT group_id FROM {TABLE}"
        params: list[Any] = []
        if state is not None:
            query += " WHERE state = ?"
            params.append(state.value)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [load_group(db_path, row[0]) for row in rows]


def update_group_state(
    db_path: str,
    group_id: str,
    state: GroupState,
    *,
    legs: list[dict[str, Any]] | None = None,
    be_state: dict[str, Any] | None = None,
    broker_ids: dict[str, Any] | None = None,
) -> None:
    """Persist a lifecycle transition + mutable state (geometry untouched)."""
    init_trade_group_store(db_path)
    now = _now_ms()
    conn = get_connection(db_path)
    try:
        sets = ["state = ?", "updated_at_ms = ?"]
        params: list[Any] = [state.value, now]
        if legs is not None:
            sets.append("legs_json = ?")
            params.append(json.dumps(legs))
        if be_state is not None:
            sets.append("be_json = ?")
            params.append(json.dumps(be_state))
        if broker_ids is not None:
            sets.append("broker_ids_json = ?")
            params.append(json.dumps(broker_ids))
        params.append(group_id)
        conn.execute(
            f"UPDATE {TABLE} SET {', '.join(sets)} WHERE group_id = ?", params
        )
        conn.commit()
    finally:
        conn.close()


def try_mark_submitted(db_path: str, group_id: str) -> bool:
    """Restart-safety guard: True only for the FIRST submit of this group.

    A recovered executor calling submit again gets False and must NOT send
    duplicate orders (ТЗ §25/§28.8).
    """
    init_trade_group_store(db_path)
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            f"""UPDATE {TABLE} SET submitted = 1
                WHERE group_id = ? AND submitted = 0""",
            (group_id,),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def is_submitted(db_path: str, group_id: str) -> bool:
    init_trade_group_store(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT submitted FROM {TABLE} WHERE group_id = ?", (group_id,)
        ).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()
