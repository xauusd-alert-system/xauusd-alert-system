"""Tests for the Signal Desk ledger endpoints in realtime/app.py."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from contracts.execution_contracts import ExecutionEvent, execution_event_id
from data.ledger_bridge import build_envelope, sign_envelope
from realtime.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_INGEST_TOKEN", "ingest-token")
    monkeypatch.setenv("LEDGER_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    return TestClient(app)


def _deal_event(deal: int, **overrides) -> ExecutionEvent:
    base = dict(
        event_id=execution_event_id("mt5_observer", "demo:7", "deal", str(deal)),
        event_type="deal_added",
        source="mt5_observer",
        account_mode="demo",
        broker_symbol="GOLD",
        asset_key="XAUUSD",
        magic_number=777111,
        deal_ticket=deal,
        fill_price=4250.5,
        requested_price=4250.0,
        filled_volume=0.1,
        spread_points=25.0,
        precision="passive",
        received_at_utc_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return ExecutionEvent(**base)


def _post(client, envelope: dict, *, token: str = "ingest-token",
          signature: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if signature:
        headers["X-Ledger-Signature"] = signature
    return client.post("/api/ledger/ingest", json=envelope, headers=headers)


def test_ingest_fails_closed_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.delenv("LEDGER_INGEST_TOKEN", raising=False)
    client = TestClient(app)
    envelope = build_envelope([_deal_event(1)], producer="mt5_observer",
                              account_mode="demo", account_login=7).model_dump(mode="json")
    assert _post(client, envelope).status_code == 403
    assert _post(client, envelope, token="wrong").status_code in (401, 403)


def test_ingest_accepts_and_dedupes(client):
    envelope = build_envelope([_deal_event(1), _deal_event(2)],
                              producer="mt5_observer", account_mode="demo",
                              account_login=7).model_dump(mode="json")
    res = _post(client, envelope)
    assert res.status_code == 200
    assert res.json()["accepted"] == 2
    # same envelope again -> duplicates, table unchanged
    res = _post(client, envelope)
    assert res.status_code == 200
    assert res.json()["duplicates"] == 2
    events = client.get("/api/ledger/events", headers={"Authorization": "Bearer owner-token"})
    assert events.json()["count"] == 2


def test_ingest_rejects_malformed_envelope(client):
    res = _post(client, {"schema_version": 1, "events": "nope"})
    assert res.status_code == 422


def test_ingest_requires_signature_when_secret_configured(client, monkeypatch):
    monkeypatch.setenv("LEDGER_INGEST_SECRET", "shared-secret")
    envelope = build_envelope([_deal_event(1)], producer="mt5_observer",
                              account_mode="demo", account_login=7)
    payload = envelope.model_dump(mode="json")
    # unsigned -> 401
    assert _post(client, payload).status_code == 401
    # signed -> 200
    signature = sign_envelope(envelope, "shared-secret")
    res = _post(client, payload, signature=signature)
    assert res.status_code == 200
    assert res.json()["signature_valid"] is True
    # wrong signature -> 401
    wrong = sign_envelope(envelope, "other")
    assert _post(client, payload, signature=wrong).status_code == 401


def test_owner_reads_require_token(client):
    assert client.get("/api/ledger/events").status_code == 403
    assert client.get("/api/ledger/execution-quality").status_code == 403
    assert client.get("/api/ledger/lifecycle/abc").status_code == 403
    assert client.get("/api/ledger/events",
                      headers={"Authorization": "Bearer owner-token"}).status_code == 200


def test_execution_quality_endpoint(client):
    envelope = build_envelope(
        [_deal_event(1, spread_points=20.0), _deal_event(2, spread_points=30.0),
         _deal_event(3, precision="probe", spread_points=10.0)],
        producer="mt5_observer", account_mode="demo", account_login=7,
    ).model_dump(mode="json")
    _post(client, envelope)
    res = client.get("/api/ledger/execution-quality",
                     headers={"Authorization": "Bearer owner-token"})
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["by_precision"]["passive"]["events"] == 2
    assert data["by_precision"]["probe"]["events"] == 1
    assert data["by_precision"]["passive"]["spread_points"]["p50"] == 25.0


def test_lifecycle_trace_endpoint(client):
    intent_id = "b" * 32
    envelope = build_envelope([
        ExecutionEvent(
            event_id=execution_event_id("mt5_python_sender", "demo:7", "intent", intent_id),
            event_type="intent_created", intent_id=intent_id,
            source="mt5_python_sender", account_mode="demo",
            broker_symbol="GOLD", asset_key="XAUUSD", magic_number=777111,
            volume_requested=0.1, precision="request",
            received_at_utc_ms=1_700_000_000_000,
        ),
        _deal_event(9, intent_id=intent_id, received_at_utc_ms=1_700_000_000_100),
    ], producer="mt5_python_sender", account_mode="demo", account_login=7).model_dump(mode="json")
    _post(client, envelope)
    res = client.get(f"/api/ledger/lifecycle/{intent_id}",
                     headers={"Authorization": "Bearer owner-token"})
    assert res.status_code == 200
    trace = res.json()
    assert trace["available"] is True
    assert [f["event_type"] for f in trace["facts"]] == ["intent_created", "deal_added"]


def test_events_endpoint_filters(client):
    envelope = build_envelope([_deal_event(1), _deal_event(2)],
                              producer="mt5_observer", account_mode="demo",
                              account_login=7).model_dump(mode="json")
    _post(client, envelope)
    res = client.get("/api/ledger/events?source=mt5_observer&event_type=deal_added&asset_key=XAUUSD",
                     headers={"Authorization": "Bearer owner-token"})
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2
    assert all(e["source"] == "mt5_observer" for e in body["events"])


def test_events_endpoint_includes_freshness(client):
    envelope = build_envelope([_deal_event(1)], producer="mt5_observer",
                              account_mode="demo", account_login=7).model_dump(mode="json")
    _post(client, envelope)
    res = client.get("/api/ledger/events", headers={"Authorization": "Bearer owner-token"})
    body = res.json()
    for key in ("freshness_status", "as_of_utc_ms", "ingest_lag_ms",
                "last_successful_at_utc_ms", "source", "mode", "coverage"):
        assert key in body, key
    assert body["freshness_status"] == "fresh"
    assert body["as_of_utc_ms"] is not None


def _assert_ws_rejected(client, url: str) -> None:
    with client.websocket_connect(url) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "UNAUTHORIZED"
        # server closes the socket right after the error frame
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_json()
        assert exc_info.value.code == 1008


def test_ws_rejects_missing_or_wrong_token(client):
    _assert_ws_rejected(client, "/ws")
    _assert_ws_rejected(client, "/ws?token=wrong-token")


def test_ws_streams_ledger_events_for_owner(client):
    envelope = build_envelope([_deal_event(1)], producer="mt5_observer",
                              account_mode="demo", account_login=7).model_dump(mode="json")
    _post(client, envelope)
    with client.websocket_connect("/ws?token=owner-token") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "events"
        assert msg["count"] >= 1
        assert msg["events"][0]["deal_ticket"] == 1
        assert msg["freshness_status"] == "fresh"
        assert "server_time_utc_ms" in msg
        assert "deployment_mode" in msg
        # next push (2s later) must NOT replay already-sent events
        msg2 = ws.receive_json()
        assert msg2["type"] == "events"
        assert msg2["count"] == 0
        assert msg2["events"] == []
