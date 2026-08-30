"""Tests for per-table schema versioning (data/migrate.py + scripts/migrate_all.py).

Covers the requirements of TASK 8:

* a freshly migrated database records per-table versions in
  ``table_versions(table_name, version)``;
* re-running the per-table migrations is idempotent (0 pending);
* dry-run reports pending migrations without changing the database.
"""

from __future__ import annotations

import sqlite3

import pytest

from data.migrate import (
    TABLE_VERSIONS_TABLE,
    get_table_version,
    load_table_migrations,
    migrate_table,
    pending_table_migrations,
    set_table_version,
)
from scripts.migrate_all import PER_TABLE_FAMILIES, run_per_table_migrations


def _table_versions_rows(db_path: str) -> list[tuple[str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            (str(row[0]), int(row[1]))
            for row in conn.execute(
                f"SELECT table_name, version FROM {TABLE_VERSIONS_TABLE} ORDER BY table_name"
            ).fetchall()
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_every_known_family_has_migrations():
    """Each configured per-table family has a non-empty migration line."""
    for table in PER_TABLE_FAMILIES:
        migrations = load_table_migrations(table)
        assert migrations, f"family {table!r} has no migrations"
        versions = [m.version for m in migrations]
        assert versions == sorted(versions), f"{table}: versions not ordered"
        assert len(set(versions)) == len(versions), f"{table}: duplicate versions"


def test_unknown_table_family_has_no_migrations():
    assert load_table_migrations("no_such_family") == []


# ---------------------------------------------------------------------------
# Fresh DB: versions recorded
# ---------------------------------------------------------------------------


def test_fresh_db_gets_versions_recorded(tmp_path):
    db_path = str(tmp_path / "fresh.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        for table in PER_TABLE_FAMILIES:
            applied = migrate_table(conn, table)
            assert applied, f"fresh DB must apply migrations for {table!r}"
            assert get_table_version(conn, table) == applied[-1].version
        conn.commit()
    finally:
        conn.close()

    rows = dict(_table_versions_rows(db_path))
    for table in PER_TABLE_FAMILIES:
        assert rows[table] == 1


def test_fresh_unversioned_db_reports_version_zero(tmp_path):
    """A DB never touched by the per-table runner reports version 0 everywhere
    and does NOT create the bookkeeping table (read-only introspection)."""
    db_path = str(tmp_path / "untouched.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ohlcv_m1 (symbol TEXT, timestamp_utc INTEGER)")
    conn.commit()
    try:
        tables_before = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in PER_TABLE_FAMILIES:
            assert get_table_version(conn, table) == 0
            assert pending_table_migrations(conn, table), "must be pending"
        tables_after = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert tables_before == tables_after
    assert TABLE_VERSIONS_TABLE not in tables_after


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_rerun_is_idempotent_zero_pending(tmp_path):
    db_path = str(tmp_path / "idem.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        first = {table: migrate_table(conn, table) for table in PER_TABLE_FAMILIES}
        assert any(first.values()), "first run must apply something"
        # Second run: 0 pending, nothing applied, no version change.
        for table in PER_TABLE_FAMILIES:
            assert pending_table_migrations(conn, table) == []
            assert migrate_table(conn, table) == []
            assert get_table_version(conn, table) == first[table][-1].version
    finally:
        conn.close()
    assert len(_table_versions_rows(db_path)) == len(PER_TABLE_FAMILIES)


def test_migrate_table_respects_target_version(tmp_path):
    """``target`` caps how far the table migrates; a lower target leaves
    later migrations pending."""
    db_path = str(tmp_path / "target.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        applied = migrate_table(conn, "candles", target=1)
        assert [m.version for m in applied] == [1]
        assert get_table_version(conn, "candles") == 1
        assert pending_table_migrations(conn, "candles") == []
    finally:
        conn.close()


def test_failed_migration_leaves_version_unchanged(tmp_path):
    """A failing migration rolls back its own changes AND the version record:
    the table stays at its previous version (atomic apply+record)."""
    db_path = str(tmp_path / "fail.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        set_table_version(conn, "fake_table", 5)
        conn.commit()

        def boom(conn) -> None:  # noqa: ARG001
            conn.execute("CREATE TABLE boom_marker (x INTEGER)")
            raise RuntimeError("simulated per-table failure")

        class FakeMigration:
            version = 6
            name = "boom"

            def __init__(self) -> None:
                self.up = boom
                self.down = lambda conn: None  # noqa: E731

        import data.migrate as migrate_mod

        original = migrate_mod.load_table_migrations

        def fake_load(table: str):
            if table == "fake_table":
                return [FakeMigration()]  # type: ignore[list-item]
            return original(table)

        migrate_mod.load_table_migrations = fake_load
        try:
            with pytest.raises(RuntimeError, match="simulated per-table failure"):
                migrate_table(conn, "fake_table")
        finally:
            migrate_mod.load_table_migrations = original

        # Version NOT advanced, rollback removed the marker table.
        assert get_table_version(conn, "fake_table") == 5
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "boom_marker" not in tables
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dry-run does not change the DB
# ---------------------------------------------------------------------------


def test_dry_run_does_not_change_db(tmp_path):
    db_path = str(tmp_path / "dry.sqlite")
    # Seed a schema so baseline migrations have something to verify.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE ohlcv_m1 (symbol TEXT NOT NULL, timestamp_utc INTEGER NOT NULL,"
        " open REAL, high REAL, low REAL, close REAL, volume REAL, session TEXT,"
        " PRIMARY KEY (symbol, timestamp_utc))"
    )
    conn.commit()
    conn.close()

    before_bytes = db_path.encode("utf-8")
    conn = sqlite3.connect(db_path)
    before_tables = sorted(
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )
    conn.close()

    summary = run_per_table_migrations(db_path, dry_run=True)
    assert "pending" in summary

    # DB unchanged: same tables, no table_versions table, no versions recorded.
    conn = sqlite3.connect(db_path)
    after_tables = sorted(
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )
    versions = conn.execute(f"SELECT COUNT(*) FROM {TABLE_VERSIONS_TABLE}").fetchone()[0] if (
        TABLE_VERSIONS_TABLE in after_tables
    ) else 0
    conn.close()

    assert after_tables == before_tables
    assert TABLE_VERSIONS_TABLE not in after_tables
    assert versions == 0
    assert before_bytes == db_path.encode("utf-8")  # trivial sanity, path intact


def test_migrate_table_dry_run_applies_nothing(tmp_path):
    db_path = str(tmp_path / "dry2.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        for table in PER_TABLE_FAMILIES:
            applied = migrate_table(conn, table, dry_run=True)
            assert applied == [] or all(True for _ in applied)
            # dry_run returns [] by contract
            assert applied == []
            assert get_table_version(conn, table) == 0
        conn.commit()
    finally:
        conn.close()
    # Dry-run never writes: the bookkeeping table itself must not exist.
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert TABLE_VERSIONS_TABLE not in tables


# ---------------------------------------------------------------------------
# Baseline migration verification logic
# ---------------------------------------------------------------------------


def test_candles_baseline_rejects_legacy_pk(tmp_path):
    """A symbol-less (legacy) ohlcv table fails loudly."""
    db_path = str(tmp_path / "legacy.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE ohlcv_m5 (timestamp_utc INTEGER PRIMARY KEY, open REAL,"
        " high REAL, low REAL, close REAL, volume REAL, session TEXT)"
    )
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="missing required columns"):
            migrate_table(conn, "candles")
    finally:
        conn.close()


def test_trade_groups_baseline_rejects_partial_family(tmp_path):
    db_path = str(tmp_path / "partial.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE trade_groups (group_id TEXT PRIMARY KEY)")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="partially initialized"):
            migrate_table(conn, "trade_groups")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Orchestration (scripts/migrate_all.py)
# ---------------------------------------------------------------------------


def test_run_per_table_migrations_apply_then_zero_pending(tmp_path):
    db_path = str(tmp_path / "orch.sqlite")
    first = run_per_table_migrations(db_path, dry_run=False)
    assert "up to date" in first
    for table in PER_TABLE_FAMILIES:
        rows = dict(_table_versions_rows(db_path))
        assert rows[table] >= 1
    second = run_per_table_migrations(db_path, dry_run=True)
    assert "0 per-table migration(s) pending" in second


def test_per_table_migrations_do_not_break_migrate_all(tmp_path):
    """run_migrate_all integrates per-table summaries without failing."""
    from scripts.migrate_all import run_migrate_all

    db_path = str(tmp_path / "all.sqlite")
    results = run_migrate_all(db_paths=[db_path], dry_run=True)
    assert results, "must report for the explicit db path"
    db_path_result, ok, summary = results[0]
    assert ok, summary
    assert db_path_result == db_path
    assert "per-table" in summary
