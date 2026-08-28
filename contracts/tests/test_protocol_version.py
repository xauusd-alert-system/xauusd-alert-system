"""ТЗ 10.4 / P2-50 — observer wire protocol version validation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from contracts.execution_contracts import (
    DEFAULT_PROTOCOL_VERSION,
    OBSERVER_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    check_protocol_version,
)
from data.ledger_bridge import build_envelope
from realtime.app import app


def test_event_with_valid_version_accepted():
    ok, err, version = check_protocol_version({"protocol_version": 1})
    assert ok is True and err == "" and version == 1


def test_event_with_unknown_protocol_version_rejected():
    ok, err, version = check_protocol_version({"protocol_version": 2})
    assert ok is False and "unsupported protocol_version 2" in err
    ok, err, _ = check_protocol_version({"protocol_version": "abc"})
    assert ok is False and "integer" in err
    ok, err, _ = check_protocol_version({"protocol_version": -1})
    assert ok is False


def test_missing_version_treated_as_v1():
    """Backward compatibility: legacy observers send no protocol_version."""
    ok, err, version = check_protocol_version({})
    assert ok is True and version == DEFAULT_PROTOCOL_VERSION == 1


def _signed_raw(raw: dict, secret: str) -> tuple[bytes, str]:
    import hashlib
    import hmac as hmac_mod
    import json
    body = json.dumps(raw).encode("utf-8")
    sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def _observer_envelope_dict():
    from contracts.execution_contracts import ExecutionEvent, execution_event_id
    event = ExecutionEvent(
        event_id=execution_event_id("mt5_observer", "demo:7", "deal", "1"),
        event_type="deal_added", broker_symbol="XAUUSD",
        received_at_utc_ms=1,
    )
    return build_envelope([event], producer="mt5_observer",
                          account_mode="demo", account_login=7).model_dump(mode="json")


def test_ingest_rejects_unknown_protocol_version(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("LEDGER_INGEST_TOKEN", "tok")
    monkeypatch.setenv("LEDGER_INGEST_SECRET", "sec")
    raw = _observer_envelope_dict()
    raw["protocol_version"] = 99
    body, sig = _signed_raw(raw, "sec")
    client = TestClient(app)
    res = client.post(
        "/api/ledger/ingest", content=body,
        headers={"Authorization": "Bearer tok", "X-Ledger-Signature": sig},
    )
    assert res.status_code == 422
    assert "unsupported protocol_version 99" in res.json()["detail"]


def test_ingest_accepts_current_protocol_version(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("LEDGER_INGEST_TOKEN", "tok")
    monkeypatch.setenv("LEDGER_INGEST_SECRET", "sec")
    raw = _observer_envelope_dict()
    raw["protocol_version"] = OBSERVER_PROTOCOL_VERSION
    body, sig = _signed_raw(raw, "sec")
    client = TestClient(app)
    res = client.post(
        "/api/ledger/ingest", content=body,
        headers={"Authorization": "Bearer tok", "X-Ledger-Signature": sig},
    )
    assert res.status_code == 200
    assert res.json()["accepted"] == 1


def test_proxy_rejects_unknown_protocol_version():
    from contracts.execution_contracts import ExecutionEvent, execution_event_id
    from scripts.run_observer_signing_proxy import validate_observer_envelope
    event = ExecutionEvent(
        event_id=execution_event_id("mt5_observer", "demo:7", "deal", "1"),
        event_type="deal_added", broker_symbol="XAUUSD",
        received_at_utc_ms=1,
    )
    raw = build_envelope([event], producer="mt5_observer",
                         account_mode="demo", account_login=7).model_dump(mode="json")
    ok, err = validate_observer_envelope(raw)
    assert ok is True  # v1 default passes
    raw["protocol_version"] = 7
    ok, err = validate_observer_envelope(raw)
    assert ok is False and "protocol_version" in err


def test_protocol_version_constants():
    assert OBSERVER_PROTOCOL_VERSION == 1
    assert SUPPORTED_PROTOCOL_VERSIONS == frozenset({1})
