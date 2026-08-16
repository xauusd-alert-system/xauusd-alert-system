"""
Producer-side ledger bridge: durable disk-backed outbox + signed HTTPS delivery.

This is the Python counterpart of the MQL5 observer's outbox (and the delivery
path the MQL5 side mirrors with ``WebRequest`` from ``OnTimer`` — never from a
trade callback). Properties required by the plan:

* append-only outbox rows; an event stays until the server answers HTTP 2xx;
* deterministic ``event_id`` makes repeated delivery idempotent server-side;
* HMAC-SHA256 signature over the canonical envelope body (secret shared with
  the server); the MQL5 observer relies on HTTPS + bearer token instead
  (no HMAC primitive is available in MQL5);
* bounded batch size and simple retry/backoff for the CLI loop.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Iterable

from contracts.execution_contracts import (
    EventEnvelope,
    ExecutionEvent,
    account_fingerprint,
)
from data.storage import get_connection

TABLE = "ledger_outbox"


def init_outbox(db_path: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_json TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            delivered_at_ms INTEGER
        )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_pending "
                     f"ON {TABLE}(delivered_at_ms, id)")
        conn.commit()
    finally:
        conn.close()


def enqueue_event(db_path: str, event: ExecutionEvent) -> str:
    """Append one fact to the durable outbox (idempotent by event_id)."""
    init_outbox(db_path)
    payload = event.model_dump_json()
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"""INSERT OR IGNORE INTO {TABLE}
                (event_id, source, event_type, event_json, created_at_ms)
                VALUES (?, ?, ?, ?, ?)""",
            (event.event_id, event.source, event.event_type, payload, time.time_ns() // 1_000_000),
        )
        conn.commit()
        return event.event_id
    finally:
        conn.close()


def pending_events(db_path: str, limit: int = 200) -> list[dict[str, Any]]:
    init_outbox(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"""SELECT id, event_id, source, event_type, event_json
                FROM {TABLE}
                WHERE delivered_at_ms IS NULL
                ORDER BY id LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [
            {"id": row[0], "event_id": row[1], "source": row[2],
             "event_type": row[3], "event_json": row[4]}
            for row in rows
        ]
    finally:
        conn.close()


def outbox_stats(db_path: str) -> dict[str, int | None]:
    init_outbox(db_path)
    conn = get_connection(db_path)
    try:
        total, pending, delivered = conn.execute(
            f"""SELECT COUNT(*),
                       SUM(CASE WHEN delivered_at_ms IS NULL THEN 1 ELSE 0 END),
                       SUM(CASE WHEN delivered_at_ms IS NOT NULL THEN 1 ELSE 0 END)
                FROM {TABLE}"""
        ).fetchone()
        return {"total": int(total or 0), "pending": int(pending or 0),
                "delivered": int(delivered or 0)}
    finally:
        conn.close()


def mark_delivered(db_path: str, event_ids: Iterable[str], delivered_at_ms: int | None = None) -> int:
    """Mark events delivered after a 2xx response; returns affected row count."""
    ids = list(event_ids)
    if not ids:
        return 0
    init_outbox(db_path)
    at = int(delivered_at_ms) if delivered_at_ms is not None else time.time_ns() // 1_000_000
    conn = get_connection(db_path)
    try:
        cursor = conn.executemany(
            f"""UPDATE {TABLE} SET delivered_at_ms = ?, attempts = attempts + 1, last_error = NULL
                WHERE event_id = ? AND delivered_at_ms IS NULL""",
            [(at, eid) for eid in ids],
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def mark_failed(db_path: str, event_ids: Iterable[str], error: str) -> int:
    ids = list(event_ids)
    if not ids:
        return 0
    init_outbox(db_path)
    conn = get_connection(db_path)
    try:
        cursor = conn.executemany(
            f"""UPDATE {TABLE} SET attempts = attempts + 1, last_error = ?
                WHERE event_id = ? AND delivered_at_ms IS NULL""",
            [(error[:500], eid) for eid in ids],
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def build_envelope(
    events: list[ExecutionEvent],
    *,
    producer: str,
    account_mode: str,
    account_login: int | str,
    sent_at_utc_ms: int | None = None,
) -> EventEnvelope:
    """Wrap facts into one deliverable envelope with a shared fingerprint."""
    return EventEnvelope(
        producer=producer,  # type: ignore[arg-type]
        account_fingerprint=account_fingerprint(account_mode, account_login),
        sent_at_utc_ms=int(sent_at_utc_ms) if sent_at_utc_ms is not None else time.time_ns() // 1_000_000,
        events=events,
    )


def sign_envelope(envelope: EventEnvelope, secret: str) -> str:
    """HMAC-SHA256 hex over the canonical envelope JSON bytes."""
    canonical = envelope.model_dump_json().encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Server-side constant-time signature check."""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def deliver_batch(
    events: list[ExecutionEvent],
    *,
    ingest_url: str,
    token: str,
    secret: str | None = None,
    producer: str = "mt5_observer",
    account_mode: str = "demo",
    account_login: int | str = "0",
    timeout: float = 10.0,
) -> tuple[bool, str, int]:
    """POST one envelope; returns (ok, response_text, http_status).

    The signature header is added only when ``secret`` is provided (the MQL5
    observer has no HMAC primitive and relies on HTTPS + bearer token).
    """
    import requests

    envelope = build_envelope(events, producer=producer, account_mode=account_mode,
                              account_login=account_login)
    body = envelope.model_dump_json().encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Ledger-Batch-Id": envelope.batch_id,
    }
    if secret:
        headers["X-Ledger-Signature"] = sign_envelope(envelope, secret)
    try:
        response = requests.post(ingest_url, data=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"transport error: {exc}", 0
    ok = 200 <= response.status_code < 300
    return ok, response.text[:1000], response.status_code


def deliver_outbox(
    db_path: str,
    *,
    ingest_url: str,
    token: str,
    secret: str | None = None,
    account_mode: str = "demo",
    account_login: int | str = "0",
    batch_size: int = 100,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Deliver one batch of pending events; never deletes undelivered rows."""
    pending = pending_events(db_path, limit=batch_size)
    if not pending:
        return {"attempted": 0, "delivered": 0, "failed": 0, "ok": True, "error": None}
    events = []
    for row in pending:
        try:
            events.append(ExecutionEvent.model_validate_json(row["event_json"]))
        except Exception as exc:  # malformed row must not block the batch
            mark_failed(db_path, [row["event_id"]], f"malformed event: {exc}")
    if not events:
        return {"attempted": 0, "delivered": 0, "failed": 0, "ok": True, "error": None}
    ok, text, status = deliver_batch(
        events, ingest_url=ingest_url, token=token, secret=secret,
        producer=events[0].source, account_mode=account_mode,
        account_login=account_login, timeout=timeout,
    )
    event_ids = [e.event_id for e in events]
    if ok:
        mark_delivered(db_path, event_ids)
        return {"attempted": len(events), "delivered": len(events), "failed": 0,
                "ok": True, "error": None, "http_status": status}
    mark_failed(db_path, event_ids, text or f"http {status}")
    return {"attempted": len(events), "delivered": 0, "failed": len(events),
            "ok": False, "error": text or f"http {status}", "http_status": status}


def run_delivery_loop(
    db_path: str,
    *,
    ingest_url: str,
    token: str,
    secret: str | None = None,
    account_mode: str = "demo",
    account_login: int | str = "0",
    interval_seconds: float = 15.0,
    batch_size: int = 100,
    max_attempts: int = 0,
    on_result=None,
) -> None:
    """Blocking delivery loop for ``scripts/run_ledger_bridge.py``.

    ``max_attempts`` bounds the number of batches per run (0 = forever). A
    failed batch is retried on the next tick; events are never dropped.
    """
    attempts = 0
    while max_attempts == 0 or attempts < max_attempts:
        attempts += 1
        result = deliver_outbox(
            db_path, ingest_url=ingest_url, token=token, secret=secret,
            account_mode=account_mode, account_login=account_login,
            batch_size=batch_size,
        )
        if on_result is not None:
            on_result(result)
        stats = outbox_stats(db_path)
        if stats["pending"] == 0:
            return
        time.sleep(interval_seconds)


def load_bridge_config(cfg: dict, env=None) -> dict[str, Any]:
    """Resolve bridge settings from config/env with fail-closed defaults."""
    env = env or os.environ
    bridge = (cfg or {}).get("ledger_bridge", {}) or {}
    ingest_url = env.get("LEDGER_INGEST_URL") or bridge.get("ingest_url")
    token = env.get("LEDGER_INGEST_TOKEN") or bridge.get("ingest_token")
    if not ingest_url:
        raise RuntimeError("LEDGER_INGEST_URL is not configured")
    if not token:
        raise RuntimeError("LEDGER_INGEST_TOKEN is not configured")
    return {
        "ingest_url": str(ingest_url),
        "token": str(token),
        "secret": env.get("LEDGER_INGEST_SECRET") or bridge.get("ingest_secret"),
        "interval_seconds": float(bridge.get("interval_seconds", 15.0)),
        "batch_size": int(bridge.get("batch_size", 100)),
    }
