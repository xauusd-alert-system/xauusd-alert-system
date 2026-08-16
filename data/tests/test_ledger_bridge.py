"""Tests for data/ledger_bridge.py (durable outbox + signed delivery)."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from contracts.execution_contracts import ExecutionEvent, execution_event_id
from data.ledger_bridge import (
    deliver_batch,
    build_envelope,
    deliver_outbox,
    enqueue_event,
    init_outbox,
    mark_delivered,
    mark_failed,
    outbox_stats,
    pending_events,
    run_delivery_loop,
    sign_envelope,
    verify_signature,
)


def _event(deal: int, *, source: str = "mt5_observer", event_type: str = "deal_added",
           received: int = 1_700_000_000_000) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=execution_event_id(source, "demo:7", "deal", str(deal)),
        event_type=event_type,
        source=source,
        account_mode="demo",
        broker_symbol="GOLD",
        asset_key="XAUUSD",
        deal_ticket=deal,
        precision="passive",
        received_at_utc_ms=received,
    )


@pytest.fixture
def outbox_db(tmp_path):
    return str(tmp_path / "outbox.sqlite")


def test_outbox_enqueue_pending_and_idempotency(outbox_db):
    first = enqueue_event(outbox_db, _event(1))
    duplicate = enqueue_event(outbox_db, _event(1))
    assert first == duplicate
    enqueue_event(outbox_db, _event(2))
    pending = pending_events(outbox_db)
    assert [p["event_id"] for p in pending] == sorted(
        [execution_event_id("mt5_observer", "demo:7", "deal", str(d)) for d in (1, 2)]
    )
    stats = outbox_stats(outbox_db)
    assert stats == {"total": 2, "pending": 2, "delivered": 0}


def test_outbox_mark_delivered_only_acknowledged(outbox_db):
    enqueue_event(outbox_db, _event(1))
    enqueue_event(outbox_db, _event(2))
    ids = [execution_event_id("mt5_observer", "demo:7", "deal", "1")]
    assert mark_delivered(outbox_db, ids) == 1
    stats = outbox_stats(outbox_db)
    assert stats["delivered"] == 1 and stats["pending"] == 1
    # double-ack is harmless
    assert mark_delivered(outbox_db, ids) == 0


def test_outbox_mark_failed_records_error(outbox_db):
    enqueue_event(outbox_db, _event(1))
    eid = execution_event_id("mt5_observer", "demo:7", "deal", "1")
    assert mark_failed(outbox_db, [eid], "http 500") == 1
    row = pending_events(outbox_db)[0]
    assert row["event_id"] == eid


def test_sign_and_verify_envelope():
    envelope = build_envelope([_event(1)], producer="mt5_python_sender",
                              account_mode="demo", account_login=7)
    signature = sign_envelope(envelope, "secret")
    body = envelope.model_dump_json().encode("utf-8")
    assert verify_signature(body, signature, "secret") is True
    assert verify_signature(body, signature, "wrong") is False
    assert verify_signature(body, None, "secret") is False
    assert verify_signature(b"tampered", signature, "secret") is False


class _IngestHandler(BaseHTTPRequestHandler):
    """Records POST bodies; responds 2xx or 500 per configuration."""

    received = []  # type: ignore[assignment]
    fail_with = None  # type: ignore[assignment]

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.received.append((self.path, dict(self.headers), body))
        if self.__class__.fail_with is not None:
            self.send_response(self.__class__.fail_with)
            self.end_headers()
            self.wfile.write(b"{\"error\": \"boom\"}")
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{\"accepted\": 1}")


@pytest.fixture
def ingest_server():
    _IngestHandler.received = []
    _IngestHandler.fail_with = None
    server = HTTPServer(("127.0.0.1", 0), _IngestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/ledger/ingest"
    yield url
    server.shutdown()
    thread.join(timeout=5)


def test_deliver_outbox_success_marks_delivered(outbox_db, ingest_server):
    enqueue_event(outbox_db, _event(1))
    result = deliver_outbox(outbox_db, ingest_url=ingest_server, token="tok",
                            secret="s3cret", account_mode="demo", account_login=7)
    assert result["ok"] is True and result["delivered"] == 1
    assert outbox_stats(outbox_db)["pending"] == 0
    path, headers, body = _IngestHandler.received[0]
    assert path == "/api/ledger/ingest"
    assert headers.get("Authorization") == "Bearer tok"
    # signature always present and over the exact body sent
    signature = headers.get("X-Ledger-Signature")
    assert signature and verify_signature(body, signature, "s3cret")
    envelope = json.loads(body.decode("utf-8"))
    assert envelope["account_fingerprint"] == "demo:7"
    assert envelope["events"][0]["deal_ticket"] == 1


def test_deliver_outbox_failure_keeps_events(outbox_db, ingest_server):
    enqueue_event(outbox_db, _event(1))
    _IngestHandler.fail_with = 500
    result = deliver_outbox(outbox_db, ingest_url=ingest_server, token="tok",
                            secret="s3cret", account_mode="demo", account_login=7)
    assert result["ok"] is False and result["failed"] == 1
    assert outbox_stats(outbox_db)["pending"] == 1
    # server recovers -> same event delivered; idempotent event_id
    _IngestHandler.fail_with = None
    result = deliver_outbox(outbox_db, ingest_url=ingest_server, token="tok",
                            secret="s3cret", account_mode="demo", account_login=7)
    assert result["ok"] is True and result["delivered"] == 1
    assert len(_IngestHandler.received) == 2


def test_deliver_outbox_transport_error_keeps_events(outbox_db):
    enqueue_event(outbox_db, _event(1))
    result = deliver_outbox(outbox_db, ingest_url="http://127.0.0.1:1/ingest",
                            token="tok", secret="s3cret",
                            account_mode="demo", account_login=7, timeout=0.5)
    assert result["ok"] is False
    assert outbox_stats(outbox_db)["pending"] == 1


def test_deliver_batch_requires_secret():
    """Missing HMAC secret -> configuration failure BEFORE any POST attempt."""
    import pytest as _pytest
    with _pytest.raises(ValueError, match="LEDGER_INGEST_SECRET"):
        deliver_batch([_event(1)], ingest_url="https://h", token="tok",
                      secret=None, account_mode="demo", account_login=7)


def test_deliver_outbox_signature_always_present(outbox_db, ingest_server):
    enqueue_event(outbox_db, _event(1))
    result = deliver_outbox(outbox_db, ingest_url=ingest_server, token="tok",
                            secret="s3cret", account_mode="demo", account_login=7)
    assert result["ok"] is True
    _, headers, body = _IngestHandler.received[0]
    signature = headers.get("X-Ledger-Signature")
    assert signature
    # P1 regression: the header value must be a plain str, not a 1-tuple
    # (a trailing comma in the header dict previously produced ("abc",) which
    # requests rejects with InvalidHeader BEFORE the request is sent).
    assert isinstance(signature, str)
    assert isinstance(headers.get("X-Ledger-Signature"), str)
    # the constructed headers must be usable by requests to prepare a real
    # HTTP request (catches header-construction bugs beyond the mocked dict)
    import requests
    prepared = requests.Request(
        "POST", "https://example.invalid/api/ledger/ingest",
        headers=headers, data=body,
    ).prepare()
    assert prepared.headers.get("X-Ledger-Signature") == signature
    # signature verifies against the EXACT body the HTTP client received
    assert verify_signature(body, signature, "s3cret")
    # a signature over a different body must NOT verify
    assert not verify_signature(body + b" ", signature, "s3cret")


def test_run_delivery_loop_drains_and_stops(outbox_db, ingest_server):
    for deal in range(1, 6):
        enqueue_event(outbox_db, _event(deal))
    calls = []
    run_delivery_loop(outbox_db, ingest_url=ingest_server, token="tok",
                      secret="s3cret", account_mode="demo", account_login=7,
                      max_attempts=10, interval_seconds=0.05, on_result=calls.append)
    assert outbox_stats(outbox_db)["pending"] == 0
    assert len(calls) >= 1
    assert calls[-1]["ok"] is True


def test_envelope_rejects_empty_events():
    with pytest.raises(Exception):
        build_envelope([], producer="mt5_observer", account_mode="demo",
                       account_login=1).model_dump()
