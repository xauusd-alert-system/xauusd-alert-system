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
    """Client with strict signed ingress configured (token + secret)."""
    monkeypatch.setenv("LEDGER_INGEST_TOKEN", "ingest-token")
    monkeypatch.setenv("LEDGER_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("LEDGER_INGEST_SECRET", "test-hmac-secret")
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


def _post(client, envelope: dict, *, token: str | None = "ingest-token", signature: str | None = None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if signature:
        headers["X-Ledger-Signature"] = signature
    return client.post("/api/ledger/ingest", json=envelope, headers=headers)


def test_ingest_fails_closed_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.delenv("LEDGER_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("LEDGER_INGEST_SECRET", raising=False)
    client = TestClient(app)
    envelope = build_envelope(
        [_deal_event(1)], producer="mt5_observer", account_mode="demo", account_login=7
    ).model_dump(mode="json")
    # no token AND no secret -> 503 (signing policy unavailable takes precedence)
    assert _post(client, envelope).status_code == 503
    assert _post(client, envelope, token="wrong").status_code == 503


def test_ingest_accepts_and_dedupes(client):
    env_obj = build_envelope(
        [_deal_event(1), _deal_event(2)], producer="mt5_observer", account_mode="demo", account_login=7
    )
    body = env_obj.model_dump_json().encode("utf-8")
    sig = sign_envelope(env_obj, "test-hmac-secret")
    res = _post(client, json.loads(body), signature=sig)
    assert res.status_code == 200
    assert res.json()["accepted"] == 2
    # same signed envelope again -> duplicates, table unchanged
    res = _post(client, json.loads(body), signature=sig)
    assert res.status_code == 200
    assert res.json()["duplicates"] == 2
    events = client.get("/api/ledger/events", headers={"Authorization": "Bearer owner-token"})
    assert events.json()["count"] == 2


def test_ingest_rejects_malformed_envelope(client):
    # signed with valid HMAC, but invalid schema -> 422 (schema checked AFTER signature)
    import hashlib
    import hmac as _hmac

    raw = b'{"schema_version": 1, "events": "nope"}'
    sig = _hmac.new(b"test-hmac-secret", raw, hashlib.sha256).hexdigest()
    res = client.post(
        "/api/ledger/ingest", content=raw, headers={"Authorization": "Bearer ingest-token", "X-Ledger-Signature": sig}
    )
    assert res.status_code == 422


def test_ingest_requires_signature_when_secret_configured(client):
    envelope = build_envelope([_deal_event(1)], producer="mt5_observer", account_mode="demo", account_login=7)
    payload = envelope.model_dump(mode="json")
    # unsigned -> 401 (bearer-only is never accepted)
    assert _post(client, payload).status_code == 401
    # signed with the fixture secret -> 200
    signature = sign_envelope(envelope, "test-hmac-secret")
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
    assert client.get("/api/ledger/events", headers={"Authorization": "Bearer owner-token"}).status_code == 200


def _signed_post_ok(client, envelope_obj):
    body = envelope_obj.model_dump_json().encode("utf-8")
    sig = sign_envelope(envelope_obj, "test-hmac-secret")
    res = _post(client, json.loads(body), signature=sig)
    assert res.status_code == 200


def test_execution_quality_endpoint(client):
    envelope_obj = build_envelope(
        [
            _deal_event(1, spread_points=20.0),
            _deal_event(2, spread_points=30.0),
            _deal_event(3, precision="probe", spread_points=10.0),
        ],
        producer="mt5_observer",
        account_mode="demo",
        account_login=7,
    )
    _signed_post_ok(client, envelope_obj)
    res = client.get("/api/ledger/execution-quality", headers={"Authorization": "Bearer owner-token"})
    assert res.status_code == 200
    data = res.json()
    assert data["available"] is True
    assert data["by_precision"]["passive"]["events"] == 2
    assert data["by_precision"]["probe"]["events"] == 1
    assert data["by_precision"]["passive"]["spread_points"]["p50"] == 25.0


def test_lifecycle_trace_endpoint(client):
    intent_id = "b" * 32
    envelope_obj = build_envelope(
        [
            ExecutionEvent(
                event_id=execution_event_id("mt5_python_sender", "demo:7", "intent", intent_id),
                event_type="intent_created",
                intent_id=intent_id,
                source="mt5_python_sender",
                account_mode="demo",
                broker_symbol="GOLD",
                asset_key="XAUUSD",
                magic_number=777111,
                volume_requested=0.1,
                precision="request",
                received_at_utc_ms=1_700_000_000_000,
            ),
            _deal_event(9, intent_id=intent_id, received_at_utc_ms=1_700_000_000_100),
        ],
        producer="mt5_python_sender",
        account_mode="demo",
        account_login=7,
    )
    _signed_post_ok(client, envelope_obj)
    res = client.get(f"/api/ledger/lifecycle/{intent_id}", headers={"Authorization": "Bearer owner-token"})
    assert res.status_code == 200
    trace = res.json()
    assert trace["available"] is True
    assert [f["event_type"] for f in trace["facts"]] == ["intent_created", "deal_added"]


def test_events_endpoint_filters(client):
    envelope_obj = build_envelope(
        [_deal_event(1), _deal_event(2)], producer="mt5_observer", account_mode="demo", account_login=7
    )
    _signed_post_ok(client, envelope_obj)
    res = client.get(
        "/api/ledger/events?source=mt5_observer&event_type=deal_added&asset_key=XAUUSD",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2
    assert all(e["source"] == "mt5_observer" for e in body["events"])


def test_events_endpoint_includes_freshness(client):
    envelope_obj = build_envelope([_deal_event(1)], producer="mt5_observer", account_mode="demo", account_login=7)
    _signed_post_ok(client, envelope_obj)
    res = client.get("/api/ledger/events", headers={"Authorization": "Bearer owner-token"})
    body = res.json()
    for key in (
        "freshness_status",
        "as_of_utc_ms",
        "ingest_lag_ms",
        "last_successful_at_utc_ms",
        "source",
        "mode",
        "coverage",
    ):
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
    envelope_obj = build_envelope([_deal_event(1)], producer="mt5_observer", account_mode="demo", account_login=7)
    _signed_post_ok(client, envelope_obj)
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


def test_provenance_audit_endpoint(tmp_path, monkeypatch):
    """P1.6 §39: the provenance audit returns the group lineage with explicit
    missing nodes — never synthetic placeholders."""
    from data.trade_group_store import save_group
    from execution.trade_group import GroupState, TradeGroupSpec

    monkeypatch.setenv("LEDGER_INGEST_TOKEN", "ingest-token")
    monkeypatch.setenv("LEDGER_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "prov.sqlite"))

    spec = TradeGroupSpec(
        group_id="TG-PROV-1",
        signal_id="SGL-PROV-1",
        intent_id="INT-PROV-1",
        asset_key="XAUUSD",
        broker_symbol="GOLD",
        mode="paper",
        side="long",
        entry={"low": 99.0, "high": 101.0, "reference": 100.0},
        geometry={
            "version": "v1",
            "unit": "price",
            "step_price": 4.0,
            "tp1": 104.0,
            "tp2": 108.0,
            "tp3": 112.0,
            "sl": 90.0,
        },
        targets=[
            {"leg": 1, "price": 104.0, "allocation": 1 / 3},
            {"leg": 2, "price": 108.0, "allocation": 1 / 3},
            {"leg": 3, "price": 112.0, "allocation": 1 / 3},
        ],
        break_even={
            "trigger": "tp1_filled",
            "raw_price_policy": "actual_fill",
            "protected_price_policy": "actual_fill_plus_cost_buffer",
            "apply_to": [2, 3],
        },
        risk={"currency": "USD", "max_cash": 50.0, "max_pct": 0.5, "estimated_loss_at_sl": 30.0, "total_volume": 0.03},
        profile_id="p1",
        model_version="v3",
        model_hash="m" * 64,
        config_hash="c" * 64,
        strategy_version="s3",
        expires_at_utc_ms=1_900_000_000_000,
        created_at_utc_ms=1_700_000_000_000,
        provenance={
            "market_snapshot_id": "MARKET:XAU:1",
            "feature_snapshot_id": "FEATURE:XAU:1",
            "model_inference_id": "INFERENCE:XAU:1",
            "model_hash": "m" * 64,
            "profile_id": "p1",
            "broker_snapshot_id": "BROKER:XAU:1",
            "cost_snapshot_id": "COST:XAU:1",
            "geometry_hash": "G" * 64,
            "provenance_hash": "P" * 64,
        },
    )
    save_group(str(tmp_path / "prov.sqlite"), spec, state=GroupState.VALIDATED)
    client = TestClient(app)
    res = client.get("/api/provenance/TG-PROV-1", headers={"Authorization": "Bearer owner-token"})
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    lineage = body["lineage"]
    assert lineage["group"]["status"] == "present"
    assert lineage["market_snapshot"]["source_id"] == "MARKET:XAU:1"
    assert lineage["cost_snapshot"]["source_id"] == "COST:XAU:1"
    assert "ledger_events" in lineage
    # a missing group is an explicit missing node
    res = client.get("/api/provenance/TG-NOPE", headers={"Authorization": "Bearer owner-token"})
    assert res.status_code == 200
    assert res.json()["lineage"]["group"]["status"] == "missing"
    # owner gate
    assert client.get("/api/provenance/TG-PROV-1").status_code == 403


# ==========================================================================
# Strict signed ingress (security contract) — P1 finding regression tests
# ==========================================================================


@pytest.fixture
def signed_client(tmp_path, monkeypatch):
    """Client with LEDGER_INGEST_SECRET configured (strict signed mode)."""
    monkeypatch.setenv("LEDGER_INGEST_TOKEN", "ingest-token")
    monkeypatch.setenv("LEDGER_OWNER_TOKEN", "owner-token")
    monkeypatch.setenv("LEDGER_INGEST_SECRET", "test-hmac-secret")
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    return TestClient(app)


def _signed_envelope(deal: int = 1, producer="mt5_observer", mode="demo"):
    envelope = build_envelope([_deal_event(deal)], producer=producer, account_mode=mode, account_login=7)
    body = envelope.model_dump_json().encode("utf-8")
    return json.loads(body), body, envelope


def _ledger_count(db_path: str) -> int:
    from data.ledger_events import read_ledger_events

    return len(read_ledger_events(db_path))


def test_ingest_requires_secret_fail_closed(tmp_path, monkeypatch):
    """No LEDGER_INGEST_SECRET -> 503, ledger empty (never bearer-only accept)."""
    monkeypatch.setenv("LEDGER_INGEST_TOKEN", "ingest-token")
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.delenv("LEDGER_INGEST_SECRET", raising=False)
    client = TestClient(app)
    envelope, _, _ = _signed_envelope(1)
    # correct bearer, NO signature
    res = _post(client, envelope)
    assert res.status_code == 503
    assert _ledger_count(str(tmp_path / "ledger.sqlite")) == 0


def test_ingest_rejects_missing_signature(signed_client, tmp_path):
    """Secret set + correct bearer + NO signature header -> 401, ledger empty."""
    db = str(tmp_path / "ledger.sqlite")
    envelope, _, _ = _signed_envelope(2)
    res = _post(signed_client, envelope)
    assert res.status_code == 401
    assert _ledger_count(db) == 0


def test_ingest_rejects_bad_signature(signed_client, tmp_path):
    db = str(tmp_path / "ledger.sqlite")
    envelope, _, env_obj = _signed_envelope(3)
    bad_sig = sign_envelope(env_obj, "wrong-secret")
    res = _post(signed_client, envelope, signature=bad_sig)
    assert res.status_code == 401
    assert _ledger_count(db) == 0


def test_ingest_requires_both_bearer_and_signature(signed_client, tmp_path):
    db = str(tmp_path / "ledger.sqlite")
    envelope, _, env_obj = _signed_envelope(4)
    sig = sign_envelope(env_obj, "test-hmac-secret")
    # wrong bearer + valid signature -> 401
    res = _post(signed_client, envelope, token="wrong", signature=sig)
    assert res.status_code == 401
    assert _ledger_count(db) == 0
    # missing bearer + valid signature -> 401
    res = _post(signed_client, envelope, token=None, signature=sig)
    assert res.status_code == 401
    assert _ledger_count(db) == 0


def test_ingest_accepts_valid_signed_envelope(signed_client, tmp_path):
    db = str(tmp_path / "ledger.sqlite")
    envelope, _, env_obj = _signed_envelope(5)
    sig = sign_envelope(env_obj, "test-hmac-secret")
    res = _post(signed_client, envelope, signature=sig)
    assert res.status_code == 200
    assert res.json()["accepted"] == 1
    from data.ledger_events import read_ledger_events

    rows = read_ledger_events(db)
    assert len(rows) == 1
    # signature_valid=True ONLY after real HMAC verification
    assert int(rows.iloc[0]["signature_valid"]) == 1
    # duplicate signed envelope -> idempotent
    res2 = _post(signed_client, envelope, signature=sig)
    assert res2.status_code == 200
    assert res2.json()["duplicates"] == 1
    assert len(read_ledger_events(db)) == 1


def test_ingest_rejects_unsigned_even_with_valid_token(signed_client, tmp_path):
    """Bearer-only remote ingestion is impossible (security contract)."""
    db = str(tmp_path / "ledger.sqlite")
    envelope, _, _ = _signed_envelope(6)
    res = _post(signed_client, envelope)  # bearer only
    assert res.status_code == 401
    assert _ledger_count(db) == 0
