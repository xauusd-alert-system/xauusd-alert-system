"""Tests for execution/schema_registry.py (ТЗ 9.1–9.2)."""
from __future__ import annotations

import pytest

# Reuse the canonical spec fixture shape from the store tests.
from data.tests.test_trade_group_store import _spec  # noqa: F401
from execution.schema_registry import (
    CURRENT_INTENT_SCHEMA,
    CURRENT_TRADE_GROUP_SCHEMA,
    SCHEMA_VERSIONS,
    UnknownSchemaVersionError,
    deserialize_intent,
    deserialize_spec,
    serialize_intent,
    serialize_spec,
)
from execution.trade_group import GroupState


def test_deserialize_spec_v1_roundtrip():
    spec = _spec()
    payload = serialize_spec(spec)
    assert payload["schema_version"] == CURRENT_TRADE_GROUP_SCHEMA
    restored = deserialize_spec(payload)
    assert restored == spec
    assert restored.schema_version == CURRENT_TRADE_GROUP_SCHEMA


def test_deserialize_spec_defaults_to_v1_for_legacy_records():
    spec = _spec()
    payload = spec.model_dump(mode="json")
    # Simulate a legacy row written before version tagging: no schema_version
    # key at all.
    payload.pop("schema_version", None)
    restored = deserialize_spec(payload)
    assert restored == spec
    assert restored.schema_version == CURRENT_TRADE_GROUP_SCHEMA


def test_deserialize_spec_unknown_version_raises():
    payload = serialize_spec(_spec())
    payload["schema_version"] = "trade-group.v999"
    with pytest.raises(UnknownSchemaVersionError):
        deserialize_spec(payload)
    # UnknownSchemaVersionError is a ValueError per ТЗ 9.1.
    with pytest.raises(ValueError):
        deserialize_spec(payload)


def test_registry_migration_chain_applies():
    """A fake v0 → v1 migration must be applied by the chain."""
    import execution.schema_registry as reg

    class SpecV0:
        VERSION = "trade-group.v0"
        MIGRATES_FROM = None

        def migrate(self, data):
            return data

    class SpecV1:
        VERSION = "trade-group.v1"
        MIGRATES_FROM = "trade-group.v0"

        def migrate(self, data):
            # Real migrations rewrite payload structure; this fake only tags.
            return dict(data)

    fake_migrations = {
        SpecV0.VERSION: SpecV0(),
        SpecV1.VERSION: SpecV1(),
    }
    payload = {"schema_version": "trade-group.v0"}
    migrated = reg._apply_chain(
        payload,
        {v: v for v in fake_migrations},
        fake_migrations,
        CURRENT_TRADE_GROUP_SCHEMA,
        "trade-group.v0",
    )
    assert migrated["schema_version"] == CURRENT_TRADE_GROUP_SCHEMA


def test_deserialize_intent_v1_roundtrip():
    spec = _spec()
    from execution.execution_intent import ExecutionIntent

    intent = ExecutionIntent.from_spec(spec)
    payload = serialize_intent(intent)
    assert payload["schema_version"] == CURRENT_INTENT_SCHEMA
    restored = deserialize_intent(payload)
    assert restored == intent


def test_deserialize_intent_legacy_untagged_defaults_to_v1():
    spec = _spec()
    from execution.execution_intent import ExecutionIntent

    intent = ExecutionIntent.from_spec(spec)
    payload = intent.model_dump(mode="json")
    payload.pop("schema_version", None)
    restored = deserialize_intent(payload)
    assert restored == intent


def test_deserialize_intent_unknown_version_raises():
    spec = _spec()
    from execution.execution_intent import ExecutionIntent

    intent = ExecutionIntent.from_spec(spec)
    payload = serialize_intent(intent)
    payload["schema_version"] = "execution-intent.v42"
    with pytest.raises(ValueError):
        deserialize_intent(payload)


def test_schema_versions_registry_shape():
    assert SCHEMA_VERSIONS["trade-group"][-1] == CURRENT_TRADE_GROUP_SCHEMA
    assert SCHEMA_VERSIONS["execution-intent"][-1] == CURRENT_INTENT_SCHEMA


def test_trade_group_store_uses_registry(tmp_path, monkeypatch):
    """load_group must deserialize through the registry: unknown versions fail
    loudly instead of being silently validated."""
    import json
    import sqlite3

    from data.trade_group_store import init_trade_group_store, load_group, save_group

    db = str(tmp_path / "groups.sqlite")
    spec = _spec()
    save_group(db, spec, state=GroupState.VALIDATED)
    loaded = load_group(db, spec.group_id)
    assert loaded["spec"] == spec

    # Corrupt the row with an unknown schema_version — the registry must
    # reject it rather than silently parsing.
    init_trade_group_store(db)
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT spec_json FROM trade_groups WHERE group_id = ?",
            (spec.group_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["schema_version"] = "trade-group.v999"
        conn.execute(
            "UPDATE trade_groups SET spec_json = ? WHERE group_id = ?",
            (json.dumps(payload), spec.group_id),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError):
        load_group(db, spec.group_id)
