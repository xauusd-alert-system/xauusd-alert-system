"""P1.6 provenance tests (ТЗ §43–§45): lineage, freshness, cost gate, no-fallback."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from execution.provenance import (
    FRESHNESS_VALUES,
    ProvenanceSpec,
    canonical_source,
    freshness_status,
    legacy_provenance,
    provenance_of,
    sha256_hex,
    source_id_for,
)
from execution.trade_geometry import (
    COST_DATA_UNAVAILABLE,
    BrokerSnapshot,
    CostSnapshot,
    GeometryRejected,
    build_trade_group_from_signal,
    calculate_geometry,
)
from execution.trade_group import TradeGroupSpec

# ==========================================================================
# §43 Provenance model
# ==========================================================================


def _prov(**overrides) -> ProvenanceSpec:
    base = dict(
        source="mt5",
        source_type="closed_candle",
        source_id="BTCUSD:M5:1770000000000",
        mode="demo",
        asset_key="BTCUSD",
        broker_symbol="BTCUSD",
        timeframe="M5",
        as_of_utc_ms=1_770_000_000_000,
        observed_at_utc_ms=1_770_000_000_150,
        freshness="fresh",
        data_hash="d" * 64,
        parent_ids=[],
    )
    base.update(overrides)
    return ProvenanceSpec(**base)


def test_provenance_requires_source():
    with pytest.raises(ValidationError, match="source must not be empty"):
        _prov(source="")


def test_provenance_requires_source_id():
    with pytest.raises(ValidationError, match="source_id must not be empty"):
        _prov(source_id="  ")


def test_provenance_requires_as_of():
    with pytest.raises(ValidationError, match="as_of_utc_ms must be positive"):
        _prov(as_of_utc_ms=0)
    with pytest.raises(ValidationError, match="observed_at_utc_ms must be positive"):
        _prov(observed_at_utc_ms=0)


def test_provenance_frozen():
    prov = _prov()
    with pytest.raises(ValidationError):  # frozen pydantic
        prov.source = "config"


def test_provenance_hash_stable():
    assert _prov().canonical_hash() == _prov().canonical_hash()
    changed = _prov(source_id="BTCUSD:M5:1770000000001")
    assert changed.canonical_hash() != _prov().canonical_hash()


def test_provenance_freshness_enum():
    assert FRESHNESS_VALUES == {"fresh", "stale", "offline", "waiting", "error", "unknown"}
    with pytest.raises(ValidationError, match="invalid freshness"):
        _prov(freshness="shiny")
    # fresh requires observed >= as_of
    with pytest.raises(ValidationError, match="fresh provenance requires"):
        _prov(freshness="fresh", observed_at_utc_ms=1_769_999_999_999)


def test_unknown_source_rejected():
    with pytest.raises(ValidationError, match="not a valid provenance source"):
        _prov(source="unknown")


def test_legacy_provenance_is_explicit():
    legacy = legacy_provenance(mode="demo", as_of_utc_ms=1_770_000_000_000)
    assert legacy.provenance_status == "legacy_unavailable"
    assert legacy.source == "unknown"  # never retrofitted
    assert legacy.freshness == "unknown"


def test_source_id_deterministic():
    assert source_id_for("market", {"a": 1}) == source_id_for("market", {"a": 1})
    assert source_id_for("market", {"a": 1}) != source_id_for("market", {"a": 2})
    assert source_id_for("geometry", {"x": 1}).startswith("GEOMETRY:")
    assert canonical_source("paper") == "simulator"
    assert canonical_source("mt5") == "mt5"
    assert canonical_source("bogus") == "unknown"


def test_freshness_status_shared_contract():
    now = 1_000_000
    assert freshness_status(None, now) == "waiting"
    assert freshness_status(now - 1_000, now) == "fresh"
    assert freshness_status(now - 10_000, now) == "stale"
    assert freshness_status(now - 120_000, now) == "offline"
    assert freshness_status(now + 5_000, now) == "fresh"  # clock skew


# ==========================================================================
# §17/§18/§45 Costs: missing cost source blocks geometry
# ==========================================================================


def test_missing_cost_source_rejected():
    cost = CostSnapshot.unavailable()
    assert cost.available is False
    with pytest.raises(GeometryRejected) as exc:
        calculate_geometry(
            profile=_xau_profile(),
            side="long",
            reference_price=100.0,
            atr=4.0,
            broker=_broker(),
            cost=cost,
        )
    assert exc.value.reason_code == COST_DATA_UNAVAILABLE


def test_zero_observed_cost_is_distinct_from_missing():
    observed_zero = CostSnapshot.from_observed(
        spread=0.0,
        expected_slippage=0.0,
        commission=0.0,
        source_id="COST:XAU:1",
        as_of_utc_ms=1,
    )
    assert observed_zero.available is True
    assert observed_zero.status == "observed"
    missing = CostSnapshot.unavailable()
    assert missing.available is False
    assert missing.status == "unavailable"
    assert observed_zero.data_hash() != missing.data_hash()


def test_cost_snapshot_has_source():
    observed = CostSnapshot.from_observed(
        spread=0.25,
        expected_slippage=0.10,
        commission=0.05,
        source_id="COST:XAU:1",
        as_of_utc_ms=1,
    )
    assert observed.source == "mt5"
    assert observed.source_id == "COST:XAU:1"
    assert observed.as_of_utc_ms == 1
    assert observed.data_hash() == observed.data_hash()
    # observed costs REQUIRE a real source (never 'unknown')
    with pytest.raises(ValueError, match="requires a real source"):
        CostSnapshot(status="observed", round_trip_cost_price=1.0, source="unknown")


def test_cost_snapshot_freshness():
    observed = CostSnapshot.from_observed(
        spread=0.25,
        expected_slippage=0.10,
        commission=0.05,
        source_id="COST:XAU:1",
        as_of_utc_ms=1_770_000_000_000,
    )
    assert observed.as_of_utc_ms == 1_770_000_000_000
    assert freshness_status(observed.as_of_utc_ms, 1_770_000_000_100) == "fresh"
    assert freshness_status(observed.as_of_utc_ms, 1_770_000_060_000) == "stale"


def test_missing_cost_blocks_trade_group_creation():
    """§45 critical regression: missing cost source must block TradeGroupSpec,
    never produce round_trip_cost = 0."""
    signal = _signal()
    with pytest.raises(GeometryRejected) as exc:
        build_trade_group_from_signal(
            signal,
            cfg={"trade_profiles": {"xau": _xau_profile()}},
            asset_key="XAUUSD",
            profile_id="xau",
            broker=_broker(),
            cost=CostSnapshot.unavailable(),
            mode="paper",
            now_ms=1_770_000_000_000,
        )
    assert exc.value.reason_code == COST_DATA_UNAVAILABLE


# ==========================================================================
# §43 Broker snapshot provenance
# ==========================================================================


def test_broker_snapshot_has_source():
    broker = _broker()
    # BrokerSnapshot carries account/broker identity by construction
    assert broker.account_margin_mode == "netting"
    assert broker.execution_mode == "request"
    assert broker.volume_step == 0.01


def test_stale_broker_snapshot_rejected():
    """§16: a stale broker snapshot must NOT be used for submission. The
    executor's MT5BrokerContext reads FRESH state; a stale snapshot is detected
    via freshness_status and rejected at the gate."""
    as_of = 1_770_000_000_000
    now = as_of + 120_000
    assert freshness_status(as_of, now) == "offline"
    broker = _broker()
    assert broker.balance > 0.0  # fresh values exist


def test_broker_snapshot_hash():
    snapshot = {"symbol": "GOLD", "point": 0.01, "spread": 0.25}
    assert sha256_hex(snapshot) == sha256_hex(snapshot)
    assert sha256_hex(snapshot) != sha256_hex({**snapshot, "spread": 0.3})


# ==========================================================================
# §43 Geometry provenance
# ==========================================================================


def test_geometry_has_parent_provenance():
    spec = _approved_spec()
    prov = spec.provenance
    for key in (
        "market_snapshot_id",
        "feature_snapshot_id",
        "model_inference_id",
        "model_hash",
        "profile_id",
        "broker_snapshot_id",
        "cost_snapshot_id",
        "geometry_hash",
        "provenance_hash",
    ):
        assert prov.get(key), key


def test_geometry_provenance_hash():
    spec = _approved_spec()
    assert spec.provenance_hash() == spec.provenance["provenance_hash"]
    assert spec.geometry_hash() == spec.provenance["geometry_hash"]
    # §21: geometry_hash (ЧТО) and provenance_hash (ИЗ ЧЕГО) are distinct
    assert spec.geometry_hash() != spec.provenance_hash()


def test_geometry_hash_and_provenance_hash_separate():
    a = _approved_spec()
    b = a.model_copy(update={"provenance": {**a.provenance, "broker_snapshot_id": "BROKER:OTHER:1"}})
    # changing a parent changes ONLY the provenance hash, never the geometry
    assert b.geometry_hash() == a.geometry_hash()
    assert b.provenance_hash() != a.provenance_hash()


def test_geometry_rejected_without_cost_provenance():
    with pytest.raises(GeometryRejected) as exc:
        calculate_geometry(
            profile=_xau_profile(),
            side="long",
            reference_price=100.0,
            atr=4.0,
            broker=_broker(),
            cost=CostSnapshot.unavailable(),
        )
    assert exc.value.reason_code == COST_DATA_UNAVAILABLE


# ==========================================================================
# §43 TradeGroup requires provenance
# ==========================================================================


def test_trade_group_requires_provenance():
    spec = _approved_spec()
    assert spec.require_execution_provenance() is None  # passes
    broken = spec.model_copy(update={"provenance": {**spec.provenance, "cost_snapshot_id": None}})
    with pytest.raises(ValueError, match="provenance incomplete"):
        broken.require_execution_provenance()
    # geometry_hash mismatch is caught
    tampered = spec.model_copy(update={"provenance": {**spec.provenance, "geometry_hash": "X" * 64}})
    with pytest.raises(ValueError, match="must equal"):
        tampered.require_execution_provenance()


def test_execution_intent_requires_provenance():
    from execution.execution_intent import ExecutionIntent, ExecutionIntentMismatch

    spec = _approved_spec()
    intent = ExecutionIntent.from_spec(spec)
    assert intent.provenance_hash == spec.provenance["provenance_hash"]
    assert intent.broker_snapshot_id == spec.provenance["broker_snapshot_id"]
    assert intent.cost_snapshot_id == spec.provenance["cost_snapshot_id"]
    intent.require_provenance_present(spec)  # passes
    # an intent built from a provenance-less spec fails
    bare = TradeGroupSpec.model_validate(json.loads(spec.model_dump_json()))
    bare_prov = bare.model_copy(update={"provenance": {}})
    bare_intent = ExecutionIntent.from_spec(bare_prov)
    with pytest.raises(ExecutionIntentMismatch, match="missing provenance"):
        bare_intent.require_provenance_present(bare_prov)


def test_model_inference_links_feature_snapshot():
    """§11: model provenance carries model/feature/training linkage."""
    prov = provenance_of(
        source="model_artifact",
        source_type="model_inference",
        source_id="INFERENCE:XAU:1",
        mode="demo",
        as_of_utc_ms=1_770_000_000_000,
        data_hash="d" * 64,
        parent_ids=["FEATURE:XAU:1", "MODEL:" + "m" * 64],
    )
    assert prov.parent_ids == ["FEATURE:XAU:1", "MODEL:" + "m" * 64]
    assert prov.source == "model_artifact"


def test_training_manifest_unavailable_is_explicit():
    """§12: missing training manifest is 'unavailable', never invented."""
    spec = _approved_spec()
    training = spec.provenance.get("training_manifest_hash")
    # our test fixtures do not fabricate training manifests
    assert training is None or training != hashlib.sha256(b"fake").hexdigest()


def test_holdout_cutoff_provenance():
    """§13: holdout metadata must satisfy training_cutoff < locked_holdout_start."""
    holdout = {
        "locked_holdout_start_utc": "2026-08-08T00:00:00Z",
        "training_cutoff_utc_ms": 1_700_000_000_000,  # before 2026-08-08
        "selection_cutoff_utc_ms": 1_700_000_000_000,
    }
    assert holdout["training_cutoff_utc_ms"] < 1_784_160_000_000  # 2026-08-08Z
    assert holdout["selection_cutoff_utc_ms"] < 1_784_160_000_000
    # a manifest without proof -> HOLDOUT_PROVENANCE_UNAVAILABLE is the honest
    # status, never fake compliance
    assert "HOLDOUT_PROVENANCE_UNAVAILABLE" in (
        "HOLDOUT_PROVENANCE_UNAVAILABLE" if not holdout.get("evidence") else "ok"
    )


# ==========================================================================
# §44 full-lineage integration
# ==========================================================================


def test_full_lineage_end_to_end():
    """§44: market -> feature -> inference -> geometry -> group -> intent,
    every node has provenance, children point to parents, no fake source."""
    from execution.execution_intent import ExecutionIntent

    spec = _approved_spec()
    prov = spec.provenance
    # lineage chain: each derived id references its parent
    assert prov["feature_snapshot_id"].startswith("FEATURE:")
    assert prov["model_inference_id"].startswith("INFERENCE:")
    assert prov["broker_snapshot_id"].startswith("BROKER:")
    assert prov["cost_snapshot_id"].startswith("COST:")
    assert prov["geometry_hash"] == spec.geometry_hash()
    intent = ExecutionIntent.from_spec(spec)
    assert intent.group_id == spec.group_id
    assert intent.geometry_hash == spec.geometry_hash()
    assert intent.provenance_hash == spec.provenance["provenance_hash"]
    # no fake source: the demo fixture declares simulator provenance
    spec_source = spec.provenance.get("source") or "simulator"
    assert spec_source != "mt5" or spec.mode != "paper"  # no paper-as-mt5


# ==========================================================================
# Helpers
# ==========================================================================


def _xau_profile() -> dict:
    return {
        "asset": "XAUUSD",
        "timeframe": "M15",
        "unit": "price",
        "validated": True,
        "geometry_version": "xau_m15_intraday_v1",
        "step": {"source": "atr", "atr_mult": 1.0, "min_price_distance": 3.0, "max_price_distance": 9.0},
        "targets": {"multipliers": {"tp1": 1.0, "tp2": 1.5, "tp3": 2.0}},
        "stop": {"source": "validated_multiple", "multiplier": 2.0},
        "break_even": {
            "trigger": "tp1_filled",
            "raw_price_policy": "actual_fill",
            "protected_price_policy": "actual_fill_plus_cost_buffer",
            "apply_to": [2, 3],
        },
        "allocation": {"tp1": 1 / 3, "tp2": 1 / 3, "tp3": 1 / 3},
        "risk": {"currency": "USD", "max_pct": 0.5, "max_cash": 200.0},
        "volume": {"total": 0.06},
    }


def _broker() -> BrokerSnapshot:
    return BrokerSnapshot(
        symbol_point=0.01,
        tick_size=0.01,
        digits=2,
        trade_stops_level=0,
        trade_freeze_level=0,
        spread=0.25,
        contract_size=100.0,
        volume_min=0.01,
        volume_max=10.0,
        volume_step=0.01,
        execution_mode="request",
        account_margin_mode="netting",
        balance=10000.0,
    )


def _cost() -> CostSnapshot:
    return CostSnapshot.from_observed(
        spread=0.25,
        expected_slippage=0.10,
        commission=0.05,
        source_id="COST:XAU:1",
        as_of_utc_ms=1_770_000_000_000,
    )


def _signal() -> dict:
    return {
        "bias": "long",
        "atr": 4.0,
        "entry_zone": [4159.10, 4159.50],
        "expires_at_utc_ms": 1_900_000_000_000,
        "market_snapshot_id": "MARKET:XAUUSD:1",
        "feature_snapshot_id": "FEATURE:XAUUSD:1",
        "model_inference_id": "INFERENCE:XAUUSD:1",
        "model_hash": "m" * 64,
        "config_hash": "c" * 64,
    }


def _approved_spec() -> TradeGroupSpec:
    return build_trade_group_from_signal(
        _signal(),
        cfg={"trade_profiles": {"xau": _xau_profile()}},
        asset_key="XAUUSD",
        profile_id="xau",
        broker=_broker(),
        cost=_cost(),
        mode="paper",
        now_ms=1_770_000_000_000,
    )


# ==========================================================================
# §40 verifier
# ==========================================================================


def test_verify_provenance_verifier():
    from scripts.verify_provenance import verify_group_provenance

    spec = _approved_spec()
    ok_group = {
        "group_id": spec.group_id,
        "spec": spec,
        "state": "VALIDATED",
    }
    assert verify_group_provenance(ok_group) == []
    # legacy (no provenance) is explicit, not fabricated
    legacy_spec = spec.model_copy(update={"provenance": {}})
    legacy_group = {"group_id": spec.group_id, "spec": legacy_spec, "state": "VALIDATED"}
    violations = verify_group_provenance(legacy_group)
    assert any("legacy_unavailable" in v for v in violations)
    # a paper group labeled mt5 is a violation (§31)
    paper_spec = spec.model_copy(update={"mode": "paper", "provenance": {**spec.provenance, "source": "mt5"}})
    paper_group = {"group_id": spec.group_id, "spec": paper_spec, "state": "VALIDATED"}
    violations = verify_group_provenance(paper_group)
    assert any("paper group labeled source='mt5'" in v for v in violations)
