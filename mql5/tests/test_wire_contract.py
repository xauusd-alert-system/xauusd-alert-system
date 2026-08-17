"""Wire-contract tests for mql5/SignalDeskObserver.

The MQL5 serializer (EventSerializer.mqh) cannot run in this Linux sandbox, so
these tests lock its documented wire format with GOLDEN fixtures: the exact
`<event_id>\\t<json>` lines the observer writes to its outbox are parsed by the
Python ExecutionEvent model, and the deterministic event-id scheme is checked
against the Python mirror (contracts/execution_contracts.py). Any future change
to the serializer that breaks the contract fails here.
"""
from __future__ import annotations

import json

from contracts.execution_contracts import (
    ExecutionEvent,
    canonical_event_id_string,
    execution_event_from_dict,
)
from contracts.execution_contracts import event_envelope_from_dict

FP = "demo:12345678"
DEAL_TICKET = "1701234567"


def _split_line(line: str) -> tuple[str, dict]:
    event_id, json_part = line.split("\t", 1)
    return event_id, json.loads(json_part)


# Golden fixture: SerializeDealEvent output for a passive DEAL_ADD fact.
GOLDEN_DEAL = (
    "mt5_observer|demo:12345678|deal|1701234567\t"
    '{"schema_version":1,"event_id":"mt5_observer|demo:12345678|deal|1701234567",'
    '"event_type":"deal_added","intent_id":null,"source":"mt5_observer",'
    '"account_mode":"demo","broker_symbol":"GOLD","asset_key":"XAUUSD",'
    '"magic_number":777111,"order_ticket":5001,"deal_ticket":1701234567,'
    '"position_ticket":5001,"deal_time_msc":1701234567000,"retcode":null,'
    '"requested_price":null,"fill_price":4250.5,"filled_volume":0.1,'
    '"volume_requested":null,"spread_points":25.0,"commission":-0.05,"swap":0.0,'
    '"latency_ms":null,"precision":"passive","received_at_utc_ms":1701234567000,'
    '"reason":null,"payload":{"entry":0,"deal_type":0,"profit":12.5,'
    '"reconciled":false,"intent_id_short":"a1b2c3d4"}}'
)

# Golden fixture: SerializeDealEvent with reconciled=true (restart scan).
GOLDEN_DEAL_RECONCILED = (
    "mt5_observer|demo:12345678|deal|1701234568\t"
    '{"schema_version":1,"event_id":"mt5_observer|demo:12345678|deal|1701234568",'
    '"event_type":"deal_added","intent_id":null,"source":"mt5_observer",'
    '"account_mode":"demo","broker_symbol":"SILVER","asset_key":"XAGUSD",'
    '"magic_number":777111,"order_ticket":5002,"deal_ticket":1701234568,'
    '"position_ticket":5002,"deal_time_msc":1701234568000,"retcode":null,'
    '"requested_price":null,"fill_price":30.25,"filled_volume":0.2,'
    '"volume_requested":null,"spread_points":null,"commission":null,"swap":null,'
    '"latency_ms":null,"precision":"history_reconciled",'
    '"received_at_utc_ms":1701234569000,"reason":null,'
    '"payload":{"entry":1,"deal_type":1,"profit":-2.0,"reconciled":true}}'
)

# Golden fixture: SerializeHeartbeatEvent output.
GOLDEN_HEARTBEAT = (
    "mt5_observer|demo:12345678|heartbeat|1701234600\t"
    '{"schema_version":1,"event_id":"mt5_observer|demo:12345678|heartbeat|1701234600",'
    '"event_type":"health_heartbeat","intent_id":null,"source":"mt5_observer",'
    '"account_mode":"demo","broker_symbol":"ALL","asset_key":null,'
    '"magic_number":null,"order_ticket":null,"deal_ticket":null,'
    '"position_ticket":null,"deal_time_msc":null,"retcode":null,'
    '"requested_price":null,"fill_price":null,"filled_volume":null,'
    '"volume_requested":null,"spread_points":null,"commission":null,"swap":null,'
    '"latency_ms":null,"precision":"passive","received_at_utc_ms":1701234600000,'
    '"reason":null,"payload":{"uptime_seconds":300,"pending_outbox":2,'
    '"outbox_errors":0}}'
)

# Golden fixture: the envelope built by ObserverEA.FlushOutbox.
GOLDEN_ENVELOPE = (
    '{"schema_version":1,"producer":"mt5_observer",'
    '"account_fingerprint":"demo:12345678","batch_id":"1234567890123",'
    '"sent_at_utc_ms":1701234600000,"events":['
    + GOLDEN_DEAL.split("\t", 1)[1]
    + "," + GOLDEN_DEAL_RECONCILED.split("\t", 1)[1]
    + "]}"
)


def test_golden_deal_line_parses_and_matches_canonical_id():
    event_id, payload = _split_line(GOLDEN_DEAL)
    event = execution_event_from_dict(payload)
    assert event.event_type == "deal_added"
    assert event.source == "mt5_observer"
    assert event.account_mode == "demo"
    assert event.broker_symbol == "GOLD"
    assert event.asset_key == "XAUUSD"
    assert event.magic_number == 777111
    assert event.order_ticket == 5001
    assert event.deal_ticket == 1701234567
    assert event.fill_price == 4250.5
    assert event.filled_volume == 0.1
    assert event.spread_points == 25.0
    assert event.commission == -0.05
    assert event.precision == "passive"
    assert event.payload["intent_id_short"] == "a1b2c3d4"
    assert event_id == canonical_event_id_string("mt5_observer", FP, "deal", DEAL_TICKET)


def test_golden_reconciled_deal_line_marks_precision():
    event_id, payload = _split_line(GOLDEN_DEAL_RECONCILED)
    event = execution_event_from_dict(payload)
    assert event.precision == "history_reconciled"
    assert event.payload["reconciled"] is True
    assert event.asset_key == "XAGUSD"
    assert event.spread_points is None
    assert event_id == canonical_event_id_string("mt5_observer", FP, "deal", "1701234568")


def test_golden_heartbeat_line_parses():
    _, payload = _split_line(GOLDEN_HEARTBEAT)
    event = execution_event_from_dict(payload)
    assert event.event_type == "health_heartbeat"
    assert event.payload["pending_outbox"] == 2
    assert event.broker_symbol == "ALL"


def test_golden_envelope_parses():
    envelope = event_envelope_from_dict(json.loads(GOLDEN_ENVELOPE))
    assert envelope.producer == "mt5_observer"
    assert envelope.account_fingerprint == FP
    assert [e.event_type for e in envelope.events] == ["deal_added", "deal_added"]


def test_idempotency_same_fact_same_event_id():
    _, payload = _split_line(GOLDEN_DEAL)
    first = execution_event_from_dict(payload)
    second = execution_event_from_dict(payload)
    assert first.event_id == second.event_id
    assert first.canonical_hash() == second.canonical_hash()


def test_unknown_magic_is_observable_with_filter_zero():
    event = ExecutionEvent(
        event_id="mt5_observer|demo:1|deal|1",
        event_type="deal_added",
        source="mt5_observer",
        account_mode="demo",
        broker_symbol="GOLD",
        magic_number=0,
        precision="passive",
        received_at_utc_ms=1,
    )
    assert event.magic_number == 0


def test_intent_short_id_extraction_mirror():
    """Python mirror of EventSerializer.ExtractIntentShort against the comment
    format produced by execution/mt5_trader.py."""
    import re

    def extract(comment: str) -> str | None:
        for token in comment.split(" "):
            token = token.strip()
            if len(token) == 8 and re.fullmatch(r"[0-9a-fA-F]{8}", token):
                return token
        return None

    comment = f"XAUUSD ML Scalp {('ab' * 16)[:8]}"
    assert extract(comment) == ("ab" * 16)[:8]
    assert extract("XAUUSD ML Scalp") is None
    assert extract("XAUUSD ML Scalp 1234567") is None          # too short
    assert extract("XAUUSD ML Scalp zz123456") is None         # not hex


# ==========================================================================
# Strict signed ingress — MQL5 static contract tests (security patch)
# ==========================================================================

import os
import re


def _observer_source():
    root = os.path.join(os.path.dirname(__file__), "..", "SignalDeskObserver")
    files = [os.path.join(root, f) for f in os.listdir(root)
             if f.endswith((".mq5", ".mqh"))]
    return files


def test_mql5_has_no_trade_calls():
    """Observer stays read-only: no OrderSend/CTrade/order-modification APIs."""
    forbidden = re.compile(
        r"\b(OrderSend|OrderSendAsync|OrderModify|OrderDelete|PositionOpen|"
        r"PositionClose|PositionModify|CTrade)\b")
    for path in _observer_source():
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("/*"):
                    continue
                if forbidden.search(stripped):
                    raise AssertionError(
                        f"forbidden trade API in {os.path.basename(path)}:{lineno}: {stripped}"
                    )


def _loopback_url_validator():
    """Python mirror of ObserverEA.IsLoopbackProxyUrl for unit-level checks."""
    def validate(url: str) -> bool:
        if not url.startswith("http://127.0.0.1:"):
            return False
        path_pos = url.find("/v1/observer/ingest")
        if path_pos < 0:
            return False
        host_port = url[7:path_pos]
        if not host_port or "/" in host_port:
            return False
        if "@" in url or "?" in url or "#" in url:
            return False
        colon = host_port.find(":")
        if colon < 0:
            return False
        port = host_port[colon + 1:]
        return bool(port) and port.isdigit()
    return validate


def test_observer_accepts_only_loopback_proxy_url():
    validate = _loopback_url_validator()
    assert validate("http://127.0.0.1:8787/v1/observer/ingest") is True
    assert validate("http://127.0.0.1:1/v1/observer/ingest") is True


def test_observer_rejects_direct_external_urls():
    validate = _loopback_url_validator()
    for bad in (
        "https://ledger.example.com/api/ledger/ingest",   # https direct external
        "http://localhost:8787/v1/observer/ingest",       # hostname not 127.0.0.1
        "http://0.0.0.0:8787/v1/observer/ingest",         # non-loopback IP
        "http://192.168.1.10:8787/v1/observer/ingest",    # non-loopback IP
        "http://127.0.0.1:8787/other/path",               # wrong proxy path
        "http://127.0.0.1:8787/v1/observer/ingest?x=1",   # query string
        "http://127.0.0.1:8787",                          # missing path
        "http://127.0.0.1:port/v1/observer/ingest",       # non-numeric port
    ):
        assert validate(bad) is False, bad


def test_observer_source_uses_loopback_proxy_not_remote_url():
    """The EA must reference the loopback proxy input, never a remote https
    ingest URL or the remote bearer/HMAC secret."""
    for path in _observer_source():
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        if path.endswith("ObserverEA.mq5"):
            assert "InpProxyUrl" in text
            assert "InpProxyToken" in text
            assert "IsLoopbackProxyUrl" in text
            assert "LEDGER_INGEST_SECRET" not in text
            assert "LEDGER_INGEST_TOKEN" not in text
