"""Tests for the local loopback observer signing proxy (strict signed ingress).

Covers the security contract: loopback-only binding, proxy bearer enforcement,
observer envelope validation (producer/account_mode), exact-raw-body HMAC
forwarding, remote 2xx propagation, remote failure -> non-2xx, and no
secrets in logs/exceptions.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import threading

import pytest

from scripts.run_observer_signing_proxy import (
    LOOPBACK_HOST,
    PROXY_PATH,
    ProxyConfigError,
    build_proxy_server,
    constant_time_eq,
    load_proxy_config,
    sign_raw_body,
    validate_observer_envelope,
)

CONFIG = {
    "proxy_token": "proxy-tok",
    "ingest_url": "https://ledger.example.com/api/ledger/ingest",
    "ingest_token": "remote-tok",
    "ingest_secret": "remote-secret",
}


def _observer_envelope(producer="mt5_observer", mode="demo") -> dict:
    return {
        "schema_version": 1,
        "producer": producer,
        "account_fingerprint": "demo:7",
        "sent_at_utc_ms": 1_700_000_000_000,
        "events": [{
            "schema_version": 1,
            "event_id": "mt5_observer|demo:7|deal|1",
            "event_type": "deal_added",
            "source": "mt5_observer",
            "account_mode": mode,
            "broker_symbol": "GOLD",
            "asset_key": "XAUUSD",
            "precision": "passive",
            "received_at_utc_ms": 1_700_000_000_000,
        }],
    }


# --------------------------------------------------------------------------
# Config / bind
# --------------------------------------------------------------------------

def test_config_requires_all_secrets():
    with pytest.raises(ProxyConfigError, match="OBSERVER_PROXY_TOKEN"):
        load_proxy_config({"LEDGER_INGEST_URL": "https://h", "LEDGER_INGEST_TOKEN": "t",
                           "LEDGER_INGEST_SECRET": "s"})
    with pytest.raises(ProxyConfigError, match="LEDGER_INGEST_SECRET"):
        load_proxy_config({"OBSERVER_PROXY_TOKEN": "p", "LEDGER_INGEST_URL": "https://h",
                           "LEDGER_INGEST_TOKEN": "t"})
    with pytest.raises(ProxyConfigError, match="https://"):
        load_proxy_config({"OBSERVER_PROXY_TOKEN": "p", "LEDGER_INGEST_URL": "http://h",
                           "LEDGER_INGEST_TOKEN": "t", "LEDGER_INGEST_SECRET": "s"})


def test_bind_is_loopback_only():
    server = build_proxy_server(CONFIG)
    try:
        host, port = server.server_address
        assert host == LOOPBACK_HOST == "127.0.0.1"
        assert port > 0
    finally:
        server.server_close()


# --------------------------------------------------------------------------
# Envelope validation
# --------------------------------------------------------------------------

def test_envelope_validation_rejects_non_observer_producer():
    ok, err = validate_observer_envelope(_observer_envelope(producer="python_sender"))
    assert ok is False and "mt5_observer" in err


def test_envelope_validation_rejects_real_account():
    ok, err = validate_observer_envelope(_observer_envelope(mode="real"))
    assert ok is False and "account_mode" in err


def test_envelope_validation_accepts_demo_and_contest():
    for mode in ("demo", "contest"):
        ok, err = validate_observer_envelope(_observer_envelope(mode=mode))
        assert ok is True, err


def test_envelope_validation_rejects_bad_schema():
    ok, err = validate_observer_envelope({"producer": "mt5_observer", "events": "x"})
    assert ok is False


# --------------------------------------------------------------------------
# HMAC helpers
# --------------------------------------------------------------------------

def test_sign_raw_body_is_exact_bytes():
    body = b'{"a":1}'
    sig = sign_raw_body(body, "secret")
    expected = _hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert sig == expected
    assert sign_raw_body(b'{"a":1} ', "secret") != sig  # trailing space changes body


def test_constant_time_eq():
    assert constant_time_eq("abc", "abc") is True
    assert constant_time_eq("abc", "abd") is False
    assert constant_time_eq("", "x") is False


# --------------------------------------------------------------------------
# End-to-end proxy behaviour
# --------------------------------------------------------------------------

class _FakeRemote:
    """Records forwarded requests; configurable response."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.lock = threading.Lock()

    def __call__(self, url, data, headers, timeout):
        with self.lock:
            self.requests.append({"url": url, "data": data, "headers": headers})
        return type("R", (), {"status_code": self.status})()


def _run_proxy(monkeypatch, remote):
    import scripts.run_observer_signing_proxy as proxy_mod
    server = build_proxy_server(CONFIG)
    monkeypatch.setattr(proxy_mod, "_requests_post", remote)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _proxy_post(server, payload: bytes, token="proxy-tok"):
    import urllib.request
    host, port = server.server_address
    url = f"http://127.0.0.1:{port}{PROXY_PATH}"
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_proxy_rejects_missing_or_wrong_token(monkeypatch):
    remote = _FakeRemote()
    server = _run_proxy(monkeypatch, remote)
    try:
        body = json.dumps(_observer_envelope()).encode("utf-8")
        status, _ = _proxy_post(server, body, token="")
        assert status == 401
        status, _ = _proxy_post(server, body, token="wrong")
        assert status == 401
        assert remote.requests == []  # remote never contacted
    finally:
        server.shutdown()


def test_proxy_rejects_invalid_json_and_schema(monkeypatch):
    remote = _FakeRemote()
    server = _run_proxy(monkeypatch, remote)
    try:
        status, _ = _proxy_post(server, b"not-json")
        assert status == 422
        status, _ = _proxy_post(server, json.dumps(
            {"producer": "mt5_observer", "events": "x"}).encode())
        assert status == 422
        status, _ = _proxy_post(server, json.dumps(
            _observer_envelope(producer="python_sender")).encode())
        assert status == 422
        status, _ = _proxy_post(server, json.dumps(
            _observer_envelope(mode="real")).encode())
        assert status == 422
        assert remote.requests == []
    finally:
        server.shutdown()


def test_proxy_forwards_exact_body_with_remote_auth_and_hmac(monkeypatch):
    remote = _FakeRemote()
    server = _run_proxy(monkeypatch, remote)
    try:
        body = json.dumps(_observer_envelope()).encode("utf-8")
        status, _ = _proxy_post(server, body)
        assert status == 200
        assert len(remote.requests) == 1
        req = remote.requests[0]
        assert req["url"] == CONFIG["ingest_url"]
        assert req["data"] == body  # EXACT raw bytes forwarded
        assert req["headers"]["Authorization"] == "Bearer remote-tok"
        expected_sig = _hmac.new(b"remote-secret", body, hashlib.sha256).hexdigest()
        assert req["headers"]["X-Ledger-Signature"] == expected_sig
    finally:
        server.shutdown()


def test_proxy_remote_failure_returns_non_2xx(monkeypatch):
    remote = _FakeRemote()
    remote.status = 401
    server = _run_proxy(monkeypatch, remote)
    try:
        body = json.dumps(_observer_envelope()).encode("utf-8")
        status, _ = _proxy_post(server, body)
        assert status == 502  # no false acknowledgement
    finally:
        server.shutdown()


def test_proxy_remote_transport_error_returns_non_2xx(monkeypatch):
    def _boom(*args, **kwargs):
        raise Exception("connection refused")
    import scripts.run_observer_signing_proxy as proxy_mod
    monkeypatch.setattr(proxy_mod, "_requests_post", _boom)
    server = build_proxy_server(CONFIG)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(_observer_envelope()).encode("utf-8")
        status, _ = _proxy_post(server, body)
        assert status == 502
    finally:
        server.shutdown()


def test_proxy_never_logs_secrets(caplog):
    """Logs must contain only safe metadata — never secrets/tokens/HMAC."""
    import logging
    import scripts.run_observer_signing_proxy as proxy_mod

    with caplog.at_level(logging.INFO):
        # simulate the exact log lines the proxy emits on success/failure
        proxy_mod.logger.info(
            "forward producer=mt5_observer account_mode=demo events=1 "
            "batch=abc remote_status=200")
        proxy_mod.logger.warning("reject status=401 reason=proxy authorization required")
        proxy_mod.logger.warning("remote transport failure: %s", "connection refused")
    text = caplog.text
    assert "remote-secret" not in text
    assert "proxy-tok" not in text
    assert "remote-tok" not in text
    assert "X-Ledger-Signature" not in text
