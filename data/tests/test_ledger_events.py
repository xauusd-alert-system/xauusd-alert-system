"""Tests for data/ledger_events.py (server-side append-only fact store)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from contracts.execution_contracts import ExecutionEvent, execution_event_id
from data.ledger_events import (
    execution_quality_summary,
    latest_ledger_activity_ms,
    lifecycle_trace,
    read_ledger_events,
    upsert_ledger_event,
)


def _fact(**overrides) -> ExecutionEvent:
    base = dict(
        event_id=execution_event_id("mt5_observer", "demo:7", "deal", "1"),
        event_type="deal_added",
        source="mt5_observer",
        account_mode="demo",
        broker_symbol="GOLD",
        asset_key="XAUUSD",
        magic_number=777111,
        deal_ticket=1,
        deal_time_msc=1_700_000_000_000,
        fill_price=4250.5,
        requested_price=4250.0,
        filled_volume=0.1,
        spread_points=25.0,
        precision="passive",
        received_at_utc_ms=1_700_000_000_500,
    )
    base.update(overrides)
    return ExecutionEvent(**base)


@pytest.fixture
def ledger_db(tmp_path):
    return str(tmp_path / "ledger.sqlite")


def test_upsert_is_idempotent_and_append_only(ledger_db):
    event_id, inserted = upsert_ledger_event(ledger_db, _fact(), signature_valid=True)
    assert inserted is True
    event_id2, inserted2 = upsert_ledger_event(ledger_db, _fact(), signature_valid=False)
    assert event_id2 == event_id
    assert inserted2 is False
    rows = read_ledger_events(ledger_db)
    assert len(rows) == 1
    assert int(rows.iloc[0]["signature_valid"]) == 1  # first write wins
    # UPDATE/DELETE are blocked by triggers
    with pytest.raises(sqlite3.IntegrityError):
        conn = sqlite3.connect(ledger_db)
        try:
            conn.execute("DELETE FROM ledger_events")
            conn.commit()
        finally:
            conn.close()


def test_read_filters(ledger_db):
    upsert_ledger_event(ledger_db, _fact(), signature_valid=True)
    upsert_ledger_event(
        ledger_db,
        _fact(
            event_id=execution_event_id("mt5_observer", "demo:7", "deal", "2"),
            deal_ticket=2,
            event_type="order_history_added",
            source="mt5_python_sender",
            precision="request",
        ),
        signature_valid=True,
    )
    assert len(read_ledger_events(ledger_db, source="mt5_observer")) == 1
    assert len(read_ledger_events(ledger_db, event_type="order_history_added")) == 1
    assert len(read_ledger_events(ledger_db, asset_key="XAUUSD")) == 2
    assert len(read_ledger_events(ledger_db, since_ms=1_700_000_000_600)) == 0


def test_execution_quality_summary_splits_precision(ledger_db):
    assert execution_quality_summary(ledger_db)["available"] is False
    for deal, precision, spread, fill, req in (
        (1, "passive", 20.0, 4250.5, 4250.0),
        (2, "passive", 30.0, 4251.0, 4250.0),
        (3, "probe", 10.0, 4250.2, 4250.0),
    ):
        upsert_ledger_event(
            ledger_db,
            _fact(
                event_id=execution_event_id("mt5_observer", "demo:7", "deal", str(deal)),
                deal_ticket=deal,
                precision=precision,
                spread_points=spread,
                fill_price=fill,
                requested_price=req,
            ),
            signature_valid=True,
        )
    summary = execution_quality_summary(ledger_db)
    assert summary["available"] is True
    assert summary["events"] == 3
    # passive vs probe never mixed
    assert summary["by_precision"]["passive"]["events"] == 2
    assert summary["by_precision"]["probe"]["events"] == 1
    assert summary["by_precision"]["passive"]["spread_points"]["p50"] == 25.0
    assert summary["by_precision"]["probe"]["spread_points"]["p50"] == 10.0
    # adverse slippage: buys fill above request = positive; p50 of [0.5, 1.0]
    assert summary["by_precision"]["passive"]["adverse_slippage_price_units"]["p50"] == 0.75


def test_lifecycle_trace(ledger_db):
    assert lifecycle_trace(ledger_db, "nope")["available"] is False
    intent_id = "a" * 32
    upsert_ledger_event(
        ledger_db,
        _fact(
            event_id=execution_event_id("mt5_python_sender", "demo:7", "intent", intent_id),
            event_type="intent_created",
            intent_id=intent_id,
            source="mt5_python_sender",
            precision="request",
            volume_requested=0.1,
            received_at_utc_ms=1_700_000_000_400,
        ),
        signature_valid=True,
    )
    upsert_ledger_event(
        ledger_db,
        _fact(
            event_id=execution_event_id("mt5_observer", "demo:7", "deal", "9"),
            intent_id=intent_id,
            deal_ticket=9,
            precision="passive",
            received_at_utc_ms=1_700_000_000_500,
        ),
        signature_valid=True,
    )
    trace = lifecycle_trace(ledger_db, intent_id)
    assert trace["available"] is True
    assert [f["event_type"] for f in trace["facts"]] == ["intent_created", "deal_added"]
    assert trace["intent"] is None  # no ledger_intents row in this DB


def test_latest_activity(ledger_db):
    assert latest_ledger_activity_ms(ledger_db) is None
    upsert_ledger_event(ledger_db, _fact(), signature_valid=True, received_at_utc_ms=1_700_000_000_500)
    assert latest_ledger_activity_ms(ledger_db) == 1_700_000_000_500


def test_payload_roundtrip(ledger_db):
    upsert_ledger_event(ledger_db, _fact(payload={"reconciled": True, "entry": 0}), signature_valid=True)
    row = read_ledger_events(ledger_db).iloc[0]
    assert json.loads(row["payload_json"]) == {"reconciled": True, "entry": 0}
