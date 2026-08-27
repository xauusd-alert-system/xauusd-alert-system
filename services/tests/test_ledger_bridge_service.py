"""Ledger Bridge service checks + entrypoint guard (TZ 8.1, P2-19)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from data.ledger_bridge import enqueue_event, init_outbox, mark_delivered
from contracts.execution_contracts import ExecutionEvent
from services.base import create_health_app
from services.ledger_bridge import service as lb


def _event(eid: str) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=eid,
        source="mt5_observer",
        event_type="health_heartbeat",
        broker_symbol="XAUUSD",
        received_at_utc_ms=time.time_ns() // 1_000_000,
        payload={"k": "v"},
    )


@pytest.fixture()
def outbox_db(tmp_path):
    db_path = str(tmp_path / "outbox.sqlite")
    init_outbox(db_path)
    return db_path


def test_ingest_db_check_missing_db_fails(tmp_path):
    check = lb.make_ingest_db_check(str(tmp_path / "nope.sqlite"))
    ok, detail = check()
    assert ok is False
    assert "not found" in detail


def test_ingest_db_check_ok_and_missing_table(outbox_db, tmp_path):
    ok, detail = lb.make_ingest_db_check(outbox_db)()
    assert ok is True
    assert detail == "ok"

    empty_db = str(tmp_path / "empty.sqlite")
    open(empty_db, "w").close()
    ok, detail = lb.make_ingest_db_check(empty_db)()
    assert ok is False
    assert "missing" in detail


def test_watermark_check_idle_outbox_is_ok(outbox_db):
    # No pending events -> nothing must move -> healthy.
    ok, detail = lb.make_watermark_check(outbox_db)()
    assert ok is True
    assert "pending=0" in detail


def test_watermark_check_fresh_delivery_is_ok(outbox_db):
    enqueue_event(outbox_db, _event("e1"))
    enqueue_event(outbox_db, _event("e2"))
    mark_delivered(outbox_db, ["e1"])  # watermark just moved
    # e2 still pending, but the watermark is fresh.
    ok, detail = lb.make_watermark_check(outbox_db)()
    assert ok is True


def test_watermark_check_never_moved_is_degraded(outbox_db):
    enqueue_event(outbox_db, _event("e1"))  # pending, no delivery ever
    ok, detail = lb.make_watermark_check(outbox_db)()
    assert ok is False
    assert "never moved" in detail


def test_watermark_check_stale_is_degraded(outbox_db):
    enqueue_event(outbox_db, _event("e1"))
    enqueue_event(outbox_db, _event("e2"))
    old_ms = time.time_ns() // 1_000_000 - int(120 * 60_000)  # 2h ago
    mark_delivered(outbox_db, ["e1"], delivered_at_ms=old_ms)
    ok, detail = lb.make_watermark_check(outbox_db, max_age_minutes=30)()
    assert ok is False
    assert "stale" in detail


def test_build_checks_health_endpoint(outbox_db):
    client = TestClient(create_health_app(lb.build_checks(outbox_db)))
    body = client.get("/health").json()
    assert set(body["checks"]) == {"ingest_db", "outbox_watermark"}
    assert body["status"] == "ok"


def test_entrypoint_argparse_guard():
    parser = lb.build_parser()
    args = parser.parse_args(["--db-path", "x.sqlite", "--once"])
    assert args.db_path == "x.sqlite"
    assert args.once is True
    assert args.health_port == lb.DEFAULT_HEALTH_PORT
    # default parses cleanly (no required args)
    assert parser.parse_args([]).account_mode == "demo"
