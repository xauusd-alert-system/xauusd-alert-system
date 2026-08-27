"""Tests for data/migrate.py — versioned schema migration runner (ТЗ §9.3)."""
from __future__ import annotations

import sqlite3

import pytest

from data.migrate import (
    MIGRATIONS_TABLE,
    Migration,
    apply_migrations,
    current_version,
    load_builtin_migrations,
    pending_migrations,
)


def test_fresh_db_applies_all_migrations(tmp_path):
    db_path = str(tmp_path / "fresh.sqlite")
    applied = apply_migrations(db_path)
    builtin = load_builtin_migrations()
    assert [m.version for m in applied] == [m.version for m in builtin]
    assert applied, "built-in migrations must be non-empty"
    # Bookkeeping rows written after successful application.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT version, name FROM {MIGRATIONS_TABLE} ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    assert [(r[0], r[1]) for r in rows] == [
        (m.version, m.name) for m in applied
    ]


def test_migrations_idempotent(tmp_path):
    db_path = str(tmp_path / "idem.sqlite")
    first = apply_migrations(db_path)
    assert first
    # Second run: nothing pending, no errors, no duplicate records.
    second = apply_migrations(db_path)
    assert second == []
    assert pending_migrations(db_path) == []
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {MIGRATIONS_TABLE}"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == len(first)


def test_dry_run_does_not_apply(tmp_path):
    db_path = str(tmp_path / "dry.sqlite")
    # Make sure some application table exists so migration 001 inspects it.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE trade_groups (group_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE trade_group_actions (group_id TEXT)")
        conn.commit()
    finally:
        conn.close()

    applied = apply_migrations(db_path, dry_run=True)
    assert applied == []
    assert pending_migrations(db_path), "dry-run must leave migrations pending"
    assert current_version(db_path) == 0
    # schema_migrations bookkeeping table itself may be created, but no
    # migration is recorded and no version advances.
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert MIGRATIONS_TABLE not in tables or conn.execute(
            f"SELECT COUNT(*) FROM {MIGRATIONS_TABLE}"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_failed_migration_rolls_back(tmp_path):
    db_path = str(tmp_path / "fail.sqlite")

    def boom(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE boom_marker (x INTEGER)")
        conn.execute("INSERT INTO boom_marker VALUES (1)")
        raise RuntimeError("simulated migration failure")

    migrations = [
        Migration(version=1, name="ok", apply=lambda conn: None),
        Migration(version=2, name="boom", apply=boom),
    ]

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        apply_migrations(db_path, migrations=migrations)

    # Version 2 is NOT recorded and its changes are rolled back; version 1
    # stays applied.
    assert current_version(db_path) == 1
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "boom_marker" not in tables
        recorded = {
            row[0] for row in conn.execute(
                f"SELECT version FROM {MIGRATIONS_TABLE}"
            )
        }
    finally:
        conn.close()
    assert recorded == {1}

    # Recovery: a fixed migration applies cleanly afterwards.
    def fixed(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE boom_marker (x INTEGER)")

    apply_migrations(db_path, migrations=[
        Migration(version=1, name="ok", apply=lambda conn: None),
        Migration(version=2, name="boom", apply=fixed),
    ])
    assert current_version(db_path) == 2


def test_migration_records_timestamp(tmp_path):
    db_path = str(tmp_path / "ts.sqlite")
    applied = apply_migrations(db_path)
    assert applied
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT version, applied_at_utc_ms FROM {MIGRATIONS_TABLE} "
            f"ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == len(applied)
    for version, ts in rows:
        assert ts > 0, f"migration {version} recorded without timestamp"


def test_migration_001_noop_on_initialized_db(tmp_path):
    """Migration 001 passes on a fully initialized store (no-op verification).

    Migration 002 (feature_store) also applies on such a database — it creates
    its own table and does not depend on the store initializers.
    """
    from data.trade_group_store import init_trade_group_store

    db_path = str(tmp_path / "stores.sqlite")
    init_trade_group_store(db_path)
    applied = apply_migrations(db_path)
    assert [m.version for m in applied] == [1, 2, 3]
    assert applied[0].name == "initial"
    assert applied[1].name == "feature_store"


def test_migration_001_rejects_partial_table_family(tmp_path):
    """A half-created trade-group family must fail loudly, not silently pass."""
    db_path = str(tmp_path / "broken.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE trade_groups (group_id TEXT PRIMARY KEY)"
        )
        # companion trade_group_actions deliberately missing
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="partially initialized"):
        apply_migrations(db_path)
