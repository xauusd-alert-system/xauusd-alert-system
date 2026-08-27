"""Tests for provenance/spec.py — ProvenanceRecordV2 (ТЗ 8.7)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from provenance.spec import (
    PROVENANCE_V2_SCHEMA_VERSION,
    ProvenanceRecordV2,
    record_from_group_row,
)

NOW_MS = 1_770_000_000_000


def make_record(**overrides) -> ProvenanceRecordV2:
    base = dict(
        group_id="TG-PROV-1",
        signal_id="SGL-PROV-1",
        feature_snapshot_id="abc123",
        config_hash="c" * 64,
        broker_snapshot={"broker_snapshot_id": "BROKER:1", "spread": 0.25},
        cost_snapshot={"cost_snapshot_id": "COST:1", "round_trip": 0.30},
        as_of_utc_ms=NOW_MS,
    )
    base.update(overrides)
    return ProvenanceRecordV2(**base)


def test_record_hash_deterministic_and_schema():
    a, b = make_record(), make_record()
    assert a.schema_version == PROVENANCE_V2_SCHEMA_VERSION
    assert a.record_hash == b.record_hash
    assert len(a.record_hash) == 64


def test_hash_changes_when_content_changes():
    a = make_record()
    b = make_record(broker_snapshot={"broker_snapshot_id": "BROKER:2"})
    assert a.record_hash != b.record_hash


def test_record_hash_recomputes_consistently():
    """compute_hash() == stored record_hash (canonical sha256_hex semantics)."""
    record = make_record()
    assert record.compute_hash() == record.record_hash


def test_required_identity_fields():
    with pytest.raises(ValidationError, match="group_id must not be empty"):
        make_record(group_id="  ")
    with pytest.raises(ValidationError, match="signal_id must not be empty"):
        make_record(signal_id="")
    with pytest.raises(ValidationError, match="config_hash must not be empty"):
        make_record(config_hash="")
    with pytest.raises(ValidationError, match="as_of_utc_ms must be positive"):
        make_record(as_of_utc_ms=0)


def test_explicit_hash_is_preserved_for_verifier():
    """An explicitly wrong record_hash must NOT be silently recomputed."""
    record = make_record(record_hash="0" * 64)
    assert record.record_hash == "0" * 64
    assert record.compute_hash() != record.record_hash


def test_from_trade_group_spec_adapter():
    """Adapter catalogs lineage from TradeGroupSpec without mutating it."""
    from execution.tests.test_trade_group_executor import make_spec

    spec = make_spec()
    spec = spec.model_copy(update={"provenance": {
        "market_snapshot_id": "MARKET:1",
        "feature_snapshot_id": "FEAT:1",
        "broker_snapshot_id": "BROKER:1",
        "cost_snapshot_id": "COST:1",
        "source": "simulator",
    }})
    original = spec.provenance
    record = ProvenanceRecordV2.from_trade_group_spec(spec)
    # spec provenance untouched (wrapper, not rewrite)
    assert spec.provenance == original
    assert record.group_id == spec.group_id
    assert record.signal_id == spec.signal_id
    assert record.config_hash == spec.config_hash
    assert record.feature_snapshot_id == "FEAT:1"
    assert record.broker_snapshot == {"broker_snapshot_id": "BROKER:1"}
    assert record.cost_snapshot == {"cost_snapshot_id": "COST:1"}
    assert record.lineage["market_snapshot_id"] == "MARKET:1"
    assert record.lineage["source"] == "simulator"
    assert record.as_of_utc_ms == spec.created_at_utc_ms


def test_record_from_group_row():
    from execution.tests.test_trade_group_executor import make_spec

    spec = make_spec()
    record = record_from_group_row({"spec": spec})
    assert record.group_id == spec.group_id
    with pytest.raises(ValueError, match="no spec"):
        record_from_group_row({})
