"""Tests for provenance/store.py + migration 003 (ТЗ 8.7)."""

from __future__ import annotations

import sqlite3

import pytest

from data.migrate import apply_migrations, current_version, load_builtin_migrations
from provenance.store import (
    PROVENANCE_RECORDS_TABLE,
    ProvenanceStore,
    resolve_store_db_path,
)
from provenance.tests.test_spec import NOW_MS, make_record


@pytest.fixture
def store(tmp_path):
    return ProvenanceStore(str(tmp_path / "prov.sqlite"))


def test_save_get_roundtrip(store):
    record = make_record()
    assert store.save(record) == record.group_id
    loaded = store.get(record.group_id)
    assert loaded is not None
    assert loaded.model_dump() == record.model_dump()


def test_get_missing_returns_none(store):
    assert store.get("TG-NOPE") is None


def test_upsert_semantics(store):
    """Same group_id twice: row is replaced, not duplicated."""
    store.save(make_record())
    updated = make_record(cost_snapshot={"cost_snapshot_id": "COST:2"})
    store.save(updated)
    loaded = store.get(updated.group_id)
    assert loaded.cost_snapshot == {"cost_snapshot_id": "COST:2"}
    conn = sqlite3.connect(store.db_path)
    try:
        count = conn.execute(f"SELECT COUNT(*) FROM {PROVENANCE_RECORDS_TABLE}").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_get_range_orders_and_filters(store):
    for i in (3, 1, 2):
        store.save(make_record(group_id=f"TG-{i}", signal_id=f"SGL-{i}", as_of_utc_ms=NOW_MS + i * 1000))
    records = store.get_range(NOW_MS + 1000, NOW_MS + 2000)
    assert [r.group_id for r in records] == ["TG-1", "TG-2"]
    assert store.get_range(NOW_MS + 10_000, NOW_MS + 20_000) == []


def test_migration_003_creates_tables(tmp_path):
    db_path = str(tmp_path / "mig.sqlite")
    applied = apply_migrations(db_path)
    versions = [m.version for m in applied]
    assert 3 in versions
    assert current_version(db_path) == max(load_builtin_migrations(), key=lambda m: m.version).version
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert PROVENANCE_RECORDS_TABLE in tables
    assert "idx_provenance_records_signal" in indexes
    assert "idx_provenance_records_as_of" in indexes


def test_migration_and_store_use_identical_ddl(tmp_path):
    """Migration and store CREATE IF NOT EXISTS produce the same schema."""
    db_path = str(tmp_path / "both.sqlite")
    apply_migrations(db_path)
    conn = sqlite3.connect(db_path)
    try:
        sql_migration = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (PROVENANCE_RECORDS_TABLE,)
        ).fetchone()[0]
    finally:
        conn.close()
    # store on the same DB must not alter or recreate the table differently
    store = ProvenanceStore(db_path)
    store.save(make_record())
    conn = sqlite3.connect(db_path)
    try:
        sql_after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (PROVENANCE_RECORDS_TABLE,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert sql_migration == sql_after


def test_resolve_store_db_path_priority(monkeypatch, tmp_path):
    cfg = {
        "provenance": {"store": {"db_path": "cfg/prov.sqlite"}},
        "general": {"db_path": "general/candles.sqlite"},
    }
    monkeypatch.delenv("PROVENANCE_STORE_DB_PATH", raising=False)
    assert resolve_store_db_path(cfg) == "cfg/prov.sqlite"
    monkeypatch.setenv("PROVENANCE_STORE_DB_PATH", "env/prov.sqlite")
    assert resolve_store_db_path(cfg) == "env/prov.sqlite"
    monkeypatch.delenv("PROVENANCE_STORE_DB_PATH")
    assert resolve_store_db_path({}) == "data/market_data_mt5.sqlite"
    assert resolve_store_db_path({"general": {"db_path": str(tmp_path / "g.sqlite")}}) == str(tmp_path / "g.sqlite")
