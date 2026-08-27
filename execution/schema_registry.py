"""
Versioned schema registry for persisted execution contracts (ТЗ 9.1–9.2).

Persisted ``TradeGroupSpec`` / ``ExecutionIntent`` rows carry a
``schema_version`` tag. This module is the SINGLE deserialization entry point:

* the version tag is read from the payload (legacy rows without one default
  to the original ``trade-group.v1`` / ``execution-intent.v1`` version);
* unknown versions raise ``ValueError`` immediately — silently guessing is
  never allowed;
* the registry applies the chain of ``migrate(data, from_version)`` steps
  needed to bring the payload up to the current schema version before
  validating it into the pydantic model (prototype per ТЗ P2-2; today v1 is
  the current version, so the chain contains one identity migration, but the
  protocol is already in place so future versions only need to add a class
  here — call sites never change).

Guarantees:

* deserialization NEVER mutates the input dict;
* registered migration functions are pure: dict in → dict out;
* the registered version list is validated at import time (each step's
  ``migrate`` target must itself be registered, and versions are contiguous).
"""
from __future__ import annotations

from typing import Any, Callable, Protocol

from execution.execution_intent import ExecutionIntent
from execution.trade_group import GROUP_SCHEMA_VERSION, TradeGroupSpec

# The oldest payload format the registry can still deserialize. Legacy rows
# written before version tagging existed carry this version implicitly.
LEGACY_SPEC_SCHEMA_VERSION = "trade-group.v1"
# NOTE: the ExecutionIntent model itself defaults schema_version to
# GROUP_SCHEMA_VERSION ("trade-group.v1"), so persisted intents carry the
# group tag — that tag IS the current intent schema version.
LEGACY_INTENT_SCHEMA_VERSION = GROUP_SCHEMA_VERSION

CURRENT_TRADE_GROUP_SCHEMA = GROUP_SCHEMA_VERSION
CURRENT_INTENT_SCHEMA = GROUP_SCHEMA_VERSION


class UnknownSchemaVersionError(ValueError):
    """Raised when a payload carries a schema version absent from the registry."""


class SchemaMigration(Protocol):
    """One registered schema version (ТЗ P2-2 prototype).

    Future versions: add a class with ``VERSION``/``MIGRATES_FROM`` and a
    ``migrate(data)`` that returns the payload upgraded to this version.
    """

    VERSION: str
    MIGRATES_FROM: str | None

    def migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        ...  # pragma: no cover - protocol


class TradeGroupSpecV1:
    """trade-group.v1 — the current TradeGroupSpec schema (identity migration)."""

    VERSION = "trade-group.v1"
    MIGRATES_FROM: str | None = None

    def migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        return data


class ExecutionIntentV1:
    """Intent schema v1 — current (identity migration).

    The ExecutionIntent model tags its payloads with GROUP_SCHEMA_VERSION
    ("trade-group.v1"), so that is the registered intent version.
    """

    VERSION = GROUP_SCHEMA_VERSION
    MIGRATES_FROM: str | None = None

    def migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        return data


# --------------------------------------------------------------------------
# Registry internals
# --------------------------------------------------------------------------

# spec migrations keyed by the version they PRODUCE.
_SPEC_MIGRATIONS: dict[str, SchemaMigration] = {
    TradeGroupSpecV1.VERSION: TradeGroupSpecV1(),
}

# intent migrations keyed by the version they PRODUCE.
_INTENT_MIGRATIONS: dict[str, SchemaMigration] = {
    ExecutionIntentV1.VERSION: ExecutionIntentV1(),
}


def _build_chain(migrations: dict[str, SchemaMigration]) -> list[str]:
    """Validate the registry and return the ordered chain of target versions.

    Requirements checked at import time:

    * exactly one registered version has ``MIGRATES_FROM is None`` (the root);
    * every non-root version's ``MIGRATES_FROM`` is itself registered;
    * the chain has no cycles (guaranteed by construction because each target
      version is visited at most once while following ``MIGRATES_FROM``).
    """
    roots = [v for v, m in migrations.items() if m.MIGRATES_FROM is None]
    if len(roots) != 1:
        raise RuntimeError(
            f"schema registry must have exactly one root version, got {roots}"
        )
    for version, migration in migrations.items():
        parent = migration.MIGRATES_FROM
        if parent is not None and parent not in migrations:
            raise RuntimeError(
                f"schema {version!r} migrates from unregistered {parent!r}"
            )
    chain: list[str] = []
    seen: set[str] = set()
    cursor: str | None = roots[0]
    while cursor is not None:
        if cursor in seen:  # pragma: no cover - defensive
            raise RuntimeError(f"cycle in schema migration chain at {cursor!r}")
        seen.add(cursor)
        chain.append(cursor)
        next_targets = [
            v for v, m in migrations.items() if m.MIGRATES_FROM == cursor
        ]
        cursor = next_targets[0] if next_targets else None
    return chain


SPEC_CHAIN: list[str] = _build_chain(_SPEC_MIGRATIONS)
INTENT_CHAIN: list[str] = _build_chain(_INTENT_MIGRATIONS)

# Ordered registry of known schema versions (ТЗ 9.1).
SCHEMA_VERSIONS: dict[str, list[str]] = {
    "trade-group": SPEC_CHAIN,
    "execution-intent": INTENT_CHAIN,
}


def _apply_chain(
    data: dict[str, Any],
    known_versions: dict[str, str],
    migrations: dict[str, SchemaMigration],
    current: str,
    default_version: str,
) -> dict[str, Any]:
    """Bring ``data`` up to ``current`` through the registered migration chain."""
    payload = dict(data)
    version = payload.get("schema_version") or default_version
    if not isinstance(version, str) or version not in known_versions:
        raise UnknownSchemaVersionError(
            f"unknown schema_version {version!r}; known versions: "
            f"{sorted(known_versions)}"
        )
    # Walk forward from the payload's version to the current one.
    chain = _build_chain(migrations)
    index = chain.index(version)
    for target in chain[index + 1:]:
        payload = migrations[target].migrate(payload)
        payload["schema_version"] = target
    if chain[-1] != current:  # pragma: no cover - defensive
        raise RuntimeError(
            f"registry chain ends at {chain[-1]!r}, expected {current!r}"
        )
    return payload


_SPEC_VERSIONS: dict[str, str] = {v: v for v in _SPEC_MIGRATIONS}
_INTENT_VERSIONS: dict[str, str] = {v: v for v in _INTENT_MIGRATIONS}


def deserialize_spec(data: dict[str, Any]) -> TradeGroupSpec:
    """Deserialize a persisted TradeGroupSpec payload into the current model.

    * payload ``schema_version`` absent → treated as ``trade-group.v1``
      (legacy records);
    * unknown version → ``ValueError``;
    * registered migrate() chain is applied before pydantic validation.
    """
    if not isinstance(data, dict):
        raise ValueError("TradeGroupSpec payload must be a dict")
    payload = _apply_chain(
        data, _SPEC_VERSIONS, _SPEC_MIGRATIONS,
        CURRENT_TRADE_GROUP_SCHEMA, LEGACY_SPEC_SCHEMA_VERSION,
    )
    return TradeGroupSpec.model_validate(payload)


def deserialize_intent(data: dict[str, Any]) -> ExecutionIntent:
    """Deserialize a persisted ExecutionIntent payload into the current model."""
    if not isinstance(data, dict):
        raise ValueError("ExecutionIntent payload must be a dict")
    payload = _apply_chain(
        data, _INTENT_VERSIONS, _INTENT_MIGRATIONS,
        CURRENT_INTENT_SCHEMA, LEGACY_INTENT_SCHEMA_VERSION,
    )
    return ExecutionIntent.model_validate(payload)


def serialize_spec(spec: TradeGroupSpec) -> dict[str, Any]:
    """Serialize a TradeGroupSpec to a versioned payload (roundtrip helper)."""
    payload = spec.model_dump(mode="json")
    payload.setdefault("schema_version", CURRENT_TRADE_GROUP_SCHEMA)
    return payload


def serialize_intent(intent: ExecutionIntent) -> dict[str, Any]:
    """Serialize an ExecutionIntent to a versioned payload (roundtrip helper)."""
    payload = intent.model_dump(mode="json")
    payload.setdefault("schema_version", CURRENT_INTENT_SCHEMA)
    return payload


# Registered migration step callable type (exported for tests / future use):
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]
