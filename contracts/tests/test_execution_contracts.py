"""Tests for contracts/execution_contracts.py (SignalIntent / ExecutionEvent v1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.execution_contracts import (
    EXECUTION_EVENT_TYPES,
    ExecutionEvent,
    SignalIntent,
    account_fingerprint,
    build_signal_intent,
    canonical_event_id_string,
    event_envelope_from_dict,
    execution_event_id,
    new_intent_id,
)


def _intent(**overrides) -> SignalIntent:
    base = dict(
        asset_key="XAUUSD",
        broker_symbol="GOLD",
        side="long",
        requested_volume=0.10,
        entry_price=4250.0,
        sl_price=4240.0,
        tp_price=4270.0,
        model_version="v3",
        config_hash="c" * 64,
        mode="research",
        magic_number=777111,
        created_at_utc_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return build_signal_intent(**base)


def _event(**overrides) -> ExecutionEvent:
    base = dict(
        event_id="mt5_observer|demo:1|deal|42",
        event_type="deal_added",
        source="mt5_observer",
        account_mode="demo",
        broker_symbol="GOLD",
        asset_key="XAUUSD",
        magic_number=777111,
        deal_ticket=42,
        deal_time_msc=1_700_000_000_123,
        fill_price=4250.5,
        filled_volume=0.1,
        spread_points=25.0,
        precision="passive",
        received_at_utc_ms=1_700_000_000_500,
    )
    base.update(overrides)
    return ExecutionEvent(**base)


def test_signal_intent_requires_positive_volume():
    with pytest.raises(ValidationError):
        _intent(requested_volume=0.0)


def test_signal_intent_geometry_must_be_consistent():
    # TP below SL for a long is impossible
    with pytest.raises(ValidationError):
        _intent(side="long", entry_price=4250.0, sl_price=4260.0, tp_price=4255.0)
    # TP above SL for a short is impossible
    with pytest.raises(ValidationError):
        _intent(side="short", entry_price=4250.0, sl_price=4260.0, tp_price=4270.0)
    # valid short geometry passes
    _intent(side="short", entry_price=4250.0, sl_price=4260.0, tp_price=4240.0)


def test_signal_intent_unsupported_mode_rejected():
    with pytest.raises((ValidationError, ValueError)):
        _intent(mode="live")


def test_signal_intent_canonical_hash_is_stable_and_id_excluded():
    a = _intent()
    b = _intent()
    assert a.intent_id != b.intent_id
    assert a.canonical_hash() == b.canonical_hash()
    same = _intent(intent_id=a.intent_id)
    assert same.canonical_hash() == a.canonical_hash()


def test_execution_event_id_is_deterministic_and_scoped():
    first = execution_event_id("mt5_observer", "demo:1", "deal", "42")
    second = execution_event_id("mt5_observer", "demo:1", "deal", "42")
    assert first == second
    assert len(first) == 64  # sha256 hex
    # different transaction id or kind -> different id
    assert first != execution_event_id("mt5_observer", "demo:1", "deal", "43")
    assert first != execution_event_id("mt5_observer", "demo:1", "order", "42")
    # different account fingerprint -> different id
    assert first != execution_event_id("mt5_observer", "demo:2", "deal", "42")
    # MQL5 emits the canonical string; Python hashes it -> same fact, stable pair
    canonical = canonical_event_id_string("mt5_observer", "demo:1", "deal", "42")
    assert first != canonical  # different forms
    assert canonical == "mt5_observer|demo:1|deal|42"


def test_canonical_id_rejects_pipe_characters():
    with pytest.raises(ValueError):
        canonical_event_id_string("a|b", "demo:1", "deal", "42")
    with pytest.raises(ValueError):
        canonical_event_id_string("mt5_observer", "demo:1", "deal", "4|2")


def test_execution_event_validates_type_and_id():
    with pytest.raises(ValidationError, match="unsupported execution event type"):
        _event(event_type="made_up")
    with pytest.raises(ValidationError, match="event_id must not be empty"):
        _event(event_id="   ")
    _event(event_id="anything-is-fine-as-opaque-pk", event_type="health_heartbeat")


def test_execution_event_types_cover_plan_facts():
    for t in (
        "deal_added",
        "order_history_added",
        "position_modified",
        "request_result",
        "preflight_checked",
        "execution_reconciled",
        "intent_created",
        "health_heartbeat",
    ):
        assert t in EXECUTION_EVENT_TYPES


def test_execution_event_hash_excludes_volatile_fields():
    a = _event()
    b = _event(event_id=a.event_id, received_at_utc_ms=a.received_at_utc_ms + 1)
    assert a.canonical_hash() == b.canonical_hash()
    c = _event(event_id="different", received_at_utc_ms=a.received_at_utc_ms)
    assert c.canonical_hash() == a.canonical_hash()


def test_envelope_roundtrip_and_validation():
    envelope = {
        "schema_version": 1,
        "producer": "mt5_observer",
        "account_fingerprint": "demo:123",
        "sent_at_utc_ms": 1_700_000_000_000,
        "events": [_event().model_dump(mode="json")],
    }
    parsed = event_envelope_from_dict(envelope)
    assert parsed.events[0].event_id == "mt5_observer|demo:1|deal|42"
    with pytest.raises(ValidationError):
        event_envelope_from_dict({**envelope, "events": []})


def test_account_fingerprint_format():
    assert account_fingerprint("demo", 123) == "demo:123"
    assert account_fingerprint("real", "777") == "real:777"


def test_build_signal_intent_defaults():
    intent = _intent()
    assert intent.schema_version == 1
    assert intent.source == "mt5_python_sender"
    assert intent.signal_id is None
    assert intent.feature_manifest_hash is None
    assert len(new_intent_id()) == 32
