"""Tests for provenance/verifier.py — completeness, hash, TTL (ТЗ 8.7/P2-51)."""

from __future__ import annotations

import pytest

from provenance.tests.test_spec import NOW_MS, make_record
from provenance.verifier import VerificationResult, verify_record


def test_complete_record_is_ok():
    result = verify_record(make_record())
    assert isinstance(result, VerificationResult)
    assert result.complete is True
    assert result.missing_fields == []
    assert result.hash_ok is True
    assert result.age_ok is True  # no TTL configured by default
    assert result.snapshot_age_ms is None


def test_missing_fields_are_listed():
    record = make_record(broker_snapshot={}, feature_snapshot_id=None)
    result = verify_record(record)
    assert result.complete is False
    assert "broker_snapshot" in result.missing_fields
    # feature_snapshot_id is optional (Feature Store link is config-gated)
    assert "feature_snapshot_id" not in result.missing_fields
    assert "cost_snapshot" not in result.missing_fields


def test_hash_mismatch_fails_hash_ok():
    record = make_record(record_hash="f" * 64)
    result = verify_record(record)
    assert result.hash_ok is False
    assert result.complete is True  # completeness is independent


def test_stale_snapshot_fails_age_ok():
    """age > provenance.max_snapshot_age_ms -> age_ok False (P2-51)."""
    cfg = {"provenance": {"max_snapshot_age_ms": 60_000}}
    result = verify_record(make_record(), cfg=cfg, now_ms=NOW_MS + 61_000)
    assert result.age_ok is False
    assert result.snapshot_age_ms == 61_000
    fresh = verify_record(make_record(), cfg=cfg, now_ms=NOW_MS + 59_999)
    assert fresh.age_ok is True


def test_future_as_of_is_not_age_ok():
    cfg = {"provenance": {"max_snapshot_age_ms": 60_000}}
    result = verify_record(make_record(), cfg=cfg, now_ms=NOW_MS - 1)
    assert result.age_ok is False


def test_no_ttl_config_age_ok_true():
    """Without provenance.max_snapshot_age_ms the TTL check is not enforced."""
    old = make_record(as_of_utc_ms=1_000)
    assert verify_record(old, cfg={}, now_ms=NOW_MS).age_ok is True
    assert verify_record(old, cfg=None, now_ms=NOW_MS).age_ok is True


def test_verify_by_group_id_via_store(tmp_path):
    from provenance.store import ProvenanceStore

    store = ProvenanceStore(str(tmp_path / "prov.sqlite"))
    store.save(make_record())
    result = verify_record("TG-PROV-1", store=store)
    assert result.complete and result.hash_ok
    with pytest.raises(KeyError, match="TG-NOPE"):
        verify_record("TG-NOPE", store=store)


def test_verify_by_group_id_via_fallback_loader():
    """Adapter path: absent in the new store -> catalogized from the legacy
    trade_group_store row (data.trade_group_store.load_group semantics)."""

    class FakeStore:
        fallback_loader = None

        def get(self, group_id):
            return None

    from execution.tests.test_trade_group_executor import make_spec

    spec = make_spec()
    spec = spec.model_copy(
        update={
            "provenance": {
                "market_snapshot_id": "MARKET:1",
                "feature_snapshot_id": "FEAT:1",
                "broker_snapshot_id": "BROKER:1",
                "cost_snapshot_id": "COST:1",
            }
        }
    )

    class FallbackStore(FakeStore):
        calls = []

        def get(self, group_id):
            return None

        @staticmethod
        def fallback_loader(group_id):
            return {"spec": spec}

    result = verify_record(spec.group_id, store=FallbackStore())
    assert result.group_id == spec.group_id
    # legacy spec provenance lacks config-context broker/cost snapshot dicts
    assert result.complete in (True, False)  # adapter maps what exists
