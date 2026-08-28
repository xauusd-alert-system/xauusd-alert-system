"""
ProvenanceSpec v1 — единый контракт происхождения фактов (ТЗ P1.6).

Главный принцип:

    NO SOURCE → NO FACT → NO APPROVED SIGNAL → NO TRADE INTENT → NO EXECUTION

Каждый значимый внешний или производный факт несёт:

    source | sourceType | sourceId | mode | asOfUtcMs | observedAtUtcMs |
    freshness | dataHash | parentIds

* ``as_of``       = к какому market/data time относится observation (§34);
* ``observed_at`` = когда система реально получила observation;
* ``data_hash``   = детерминированный hash содержимого snapshot;
* ``parent_ids``  = lineage к родительским артефактам.

Правила:

* freshness — ЕДИНЫЙ набор статусов (fresh/stale/offline/waiting/error/unknown),
  совместимый с ``realtime.data_envelope`` (§33/§46);
* ``source="unknown"`` никогда не считается валидным источником (§37);
* legacy-записи не получают задним числом выдуманный provenance
  (``provenance_status="legacy_unavailable"``, §38);
* model config frozen — immutable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, model_validator

PROVENANCE_SCHEMA_VERSION = "provenance.v1"

FRESHNESS_VALUES = frozenset({"fresh", "stale", "offline", "waiting", "error", "unknown"})
SOURCE_TYPE_VALUES = frozenset(
    {
        "closed_candle",
        "broker_snapshot",
        "market_snapshot",
        "feature_snapshot",
        "model_artifact",
        "model_inference",
        "trade_profile",
        "training_manifest",
        "cost_snapshot",
        "derived",
        "order",
        "deal",
        "position",
        "ledger",
        "fake_mt5",
        "paper_driver",
        "config",
    }
)

# Единый source mapping (§47): класс источника -> каноническое source-значение.
SOURCE_MAPPING = {
    "mt5": "mt5",
    "simulator": "simulator",
    "paper": "simulator",
    "model_artifact": "model_artifact",
    "config": "config",
    "derived": "derived",
    "ledger": "ledger",
}


def canonical_source(kind: str) -> str:
    """Normalize a source class to the canonical source value (unknown-safe)."""
    return SOURCE_MAPPING.get(kind, "unknown")


def sha256_hex(payload: Any) -> str:
    """Deterministic sha256 over a JSON-stable payload."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def source_id_for(kind: str, value: Any) -> str:
    """Deterministic source id per class (§5): e.g. MARKET:<hash>."""
    prefix = {
        "market": "MARKET",
        "feature": "FEATURE",
        "broker": "BROKER",
        "cost": "COST",
        "model": "MODEL",
        "profile": "PROFILE",
        "geometry": "GEOMETRY",
        "group": "GROUP",
        "inference": "INFERENCE",
        "training": "TRAINING",
    }.get(kind, "SRC")
    return f"{prefix}:{sha256_hex(value)}"


class ProvenanceSpec(BaseModel):
    """Immutable provenance contract (ТЗ §3/§4)."""

    model_config = {"frozen": True}

    schema_version: str = PROVENANCE_SCHEMA_VERSION
    source: str
    source_type: str
    source_id: str
    mode: str
    asset_key: str | None = None
    broker_symbol: str | None = None
    timeframe: str | None = None
    as_of_utc_ms: int
    observed_at_utc_ms: int
    freshness: str
    data_hash: str | None = None
    parent_ids: list[str] = Field(default_factory=list)
    provenance_status: str = "available"

    @model_validator(mode="after")
    def validate_provenance(self):
        # §4: required non-empty identity
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if not self.source_type.strip():
            raise ValueError("source_type must not be empty")
        if not self.source_id.strip():
            raise ValueError("source_id must not be empty")
        if not self.mode.strip():
            raise ValueError("mode must not be empty")
        # §4: time bounds
        if self.as_of_utc_ms <= 0:
            raise ValueError("as_of_utc_ms must be positive")
        if self.observed_at_utc_ms <= 0:
            raise ValueError("observed_at_utc_ms must be positive")
        # §4/§33: единый набор freshness-статусов
        if self.freshness not in FRESHNESS_VALUES:
            raise ValueError(f"invalid freshness {self.freshness!r}; expected one of {sorted(FRESHNESS_VALUES)}")
        # §37: unknown никогда не валиден как source — кроме явного legacy-маркера
        # (§38): старые записи честно помечаются legacy_unavailable, а не
        # получают задним числом выдуманный источник.
        if self.source == "unknown" and self.provenance_status != "legacy_unavailable":
            raise ValueError("source='unknown' is not a valid provenance source")
        if self.freshness == "fresh" and self.observed_at_utc_ms < self.as_of_utc_ms:
            # fresh требует соответствия observation timestamp (§4)
            raise ValueError("fresh provenance requires observed_at_utc_ms >= as_of_utc_ms")
        return self

    def canonical_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", exclude={"schema_version"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provenance_of(
    *,
    source: str,
    source_type: str,
    source_id: str,
    mode: str,
    as_of_utc_ms: int,
    observed_at_utc_ms: int | None = None,
    freshness: str = "fresh",
    data_hash: str | None = None,
    parent_ids: list[str] | None = None,
    asset_key: str | None = None,
    broker_symbol: str | None = None,
    timeframe: str | None = None,
    provenance_status: str = "available",
) -> ProvenanceSpec:
    """Builder: observed_at defaults to as_of (same-instant observations)."""
    observed = int(observed_at_utc_ms) if observed_at_utc_ms is not None else int(as_of_utc_ms)
    return ProvenanceSpec(
        source=source,
        source_type=source_type,
        source_id=source_id,
        mode=mode,
        asset_key=asset_key,
        broker_symbol=broker_symbol,
        timeframe=timeframe,
        as_of_utc_ms=int(as_of_utc_ms),
        observed_at_utc_ms=observed,
        freshness=freshness,
        data_hash=data_hash,
        parent_ids=list(parent_ids or []),
        provenance_status=provenance_status,
    )


def legacy_provenance(*, mode: str, as_of_utc_ms: int | None = None) -> ProvenanceSpec:
    """§38: legacy records carry explicit ``legacy_unavailable`` — never a
    retrofitted fake source."""
    now = int(as_of_utc_ms) if as_of_utc_ms is not None else 0
    return ProvenanceSpec(
        source="unknown",
        source_type="legacy",
        source_id="LEGACY:unavailable",
        mode=mode,
        as_of_utc_ms=now if now > 0 else 1,
        observed_at_utc_ms=now if now > 0 else 1,
        freshness="unknown",
        provenance_status="legacy_unavailable",
    )


def freshness_status(age_ms: int | None, now_ms: int, fresh_after_ms: int = 5_000, stale_after_ms: int = 60_000) -> str:
    """Единый freshness-контракт (§33) — совместим с realtime.data_envelope."""
    if age_ms is None:
        return "waiting"
    age = int(now_ms) - int(age_ms)
    if age < 0:
        return "fresh"
    if age <= fresh_after_ms:
        return "fresh"
    if age <= stale_after_ms:
        return "stale"
    return "offline"
