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


ACTIONS_TABLE = "trade_group_actions"


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
            updated_at_ms INTEGER NOT NULL,
            intent_json TEXT,
            account_mode TEXT,
            volume_json TEXT,
            comp_json TEXT
        )""")
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
        for column in ("intent_json", "account_mode", "volume_json", "comp_json"):
            if column not in existing:
                conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} TEXT")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {ACTIONS_TABLE} (
            group_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            PRIMARY KEY (group_id, action_id)
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
    intent_json: str | None = None,
    account_mode: str | None = None,
    volume: dict[str, Any] | None = None,
    comp_state: dict[str, Any] | None = None,
) -> None:
    """Insert or update one group (state is mutable; geometry is not).

    INSERT OR REPLACE would wipe unspecified mutable columns (volume/comp/
    intent/account_mode) on every state transition, so omitted values are
    preserved from the existing row.
    """
    init_trade_group_store(db_path)
    now = _now_ms()
    existing_row = None
    try:
        existing_row = load_group(db_path, spec.group_id)
    except Exception:
        existing_row = None
    if existing_row is not None:
        if volume is None:
            volume = existing_row.get("volume") or {}
        if comp_state is None:
            comp_state = existing_row.get("comp_state") or {}
        if intent_json is None:
            intent_json = existing_row.get("intent_json")
        if account_mode is None:
            account_mode = existing_row.get("account_mode")
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"""INSERT OR REPLACE INTO {TABLE}
                (group_id, schema_version, spec_json, spec_hash, state, legs_json,
                 be_json, broker_ids_json, submitted, created_at_ms, updated_at_ms,
                 intent_json, account_mode, volume_json, comp_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                spec.group_id, spec.schema_version,
                spec.model_dump_json(), spec.geometry_hash(),
                state.value, json.dumps(legs or []),
                json.dumps(be_state or {}), json.dumps(broker_ids or {}),
                1 if submitted else 0, now, now,
                intent_json, account_mode,
                json.dumps(volume or {}), json.dumps(comp_state or {}),
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
    data["intent_json"] = data.pop("intent_json", None)
    data["account_mode"] = data.pop("account_mode", None)
    data["volume"] = json.loads(data.pop("volume_json") or "{}")
    data["comp_state"] = json.loads(data.pop("comp_json") or "{}")
    return data


# --------------------------------------------------------------------------
# Idempotent execution actions (ТЗ P1.5 §30)
# --------------------------------------------------------------------------

def mark_action(db_path: str, group_id: str, action_id: str,
                payload: dict[str, Any] | None = None) -> bool:
    """Atomically record one actionId; returns True ONLY for the first caller.

    Repeating the same ``actionId`` (restart, retry, duplicate event) never
    creates a duplicate broker action.
    """
    init_trade_group_store(db_path)
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            f"""INSERT OR IGNORE INTO {ACTIONS_TABLE}
                (group_id, action_id, payload_json, created_at_ms)
                VALUES (?, ?, ?, ?)""",
            (group_id, action_id, json.dumps(payload or {}, sort_keys=True, default=str),
             _now_ms()),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def has_action(db_path: str, group_id: str, action_id: str) -> bool:
    init_trade_group_store(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT 1 FROM {ACTIONS_TABLE} WHERE group_id=? AND action_id=?",
            (group_id, action_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_actions(db_path: str, group_id: str) -> list[dict[str, Any]]:
    init_trade_group_store(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT action_id, payload_json, created_at_ms FROM {ACTIONS_TABLE} "
            f"WHERE group_id=? ORDER BY created_at_ms, action_id",
            (group_id,),
        ).fetchall()
        return [{"action_id": r[0], "payload": json.loads(r[1] or "{}"),
                 "created_at_ms": r[2]} for r in rows]
    finally:
        conn.close()


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
    volume: dict[str, Any] | None = None,
    comp_state: dict[str, Any] | None = None,
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
        if volume is not None:
            sets.append("volume_json = ?")
            params.append(json.dumps(volume))
        if comp_state is not None:
            sets.append("comp_json = ?")
            params.append(json.dumps(comp_state))
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
