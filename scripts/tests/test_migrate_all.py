"""Tests for scripts/migrate_all.py (ТЗ 9.11)."""

from __future__ import annotations

import json
import sqlite3

from data.tests.test_trade_group_store import _spec  # noqa: F401
from scripts.migrate_all import registry_check, run_migrate_all


def _make_group_db(db_path: str, *, corrupt: bool = False) -> None:
    """Create a DB with a trade_groups row (optionally corrupt payload)."""
    from data.trade_group_store import save_group
    from execution.trade_group import GroupState

    save_group(db_path, _spec(), state=GroupState.VALIDATED)
    if corrupt:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT spec_json FROM trade_groups WHERE group_id = ?",
                (_spec().group_id,),
            ).fetchone()
            payload = json.loads(row[0])
            payload["schema_version"] = "trade-group.v999"
            conn.execute(
                "UPDATE trade_groups SET spec_json = ? WHERE group_id = ?",
                (json.dumps(payload), _spec().group_id),
            )
            conn.commit()
        finally:
            conn.close()


def test_dry_run_does_not_change_data(tmp_path):
    db_path = str(tmp_path / "dry.sqlite")
    _make_group_db(db_path)

    # Snapshot data before.
    conn = sqlite3.connect(db_path)
    try:
        before_rows = conn.execute("SELECT group_id, spec_json, state FROM trade_groups").fetchall()
        has_migrations_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
    finally:
        conn.close()

    results = run_migrate_all(db_paths=[db_path], dry_run=True)
    # Registry check runs (read-only) — must pass; migration step reports dry-run.
    statuses = {name: ok for name, ok, _ in results if name == db_path}
    assert all(statuses.values()), results

    # Data unchanged: no migration applied, no records written.
    conn = sqlite3.connect(db_path)
    try:
        after_rows = conn.execute("SELECT group_id, spec_json, state FROM trade_groups").fetchall()
        applied = (
            conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            else 0
        )
    finally:
        conn.close()
    assert after_rows == before_rows
    assert applied == 0
    assert has_migrations_table is None or applied == 0


def test_real_run_applies_migrations(tmp_path):
    db_path = str(tmp_path / "real.sqlite")
    _make_group_db(db_path)

    results = run_migrate_all(db_paths=[db_path], dry_run=False)
    entries = [(ok, summary) for name, ok, summary in results if name == db_path]
    # Migration step succeeded and recorded versions 1-3 (002 = feature_store,
    # 003 = provenance_store).
    conn = sqlite3.connect(db_path)
    try:
        applied = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    finally:
        conn.close()
    assert applied == [(1, "initial"), (2, "feature_store"), (3, "provenance_store")]
    # Registry check on the healthy payload passed.
    migration_ok, _ = entries[0]
    assert migration_ok


def test_registry_check_passes_on_correct_records(tmp_path):
    db_path = str(tmp_path / "good.sqlite")
    _make_group_db(db_path)
    check = registry_check(db_path)
    assert check.ok
    assert check.specs_checked == 1
    assert check.errors == []


def test_registry_check_fails_on_corrupt_records(tmp_path):
    db_path = str(tmp_path / "bad.sqlite")
    _make_group_db(db_path, corrupt=True)
    check = registry_check(db_path)
    assert not check.ok
    assert check.specs_checked == 1
    assert check.errors, "corrupt payload must produce an error"
    assert "trade-group.v999" in check.errors[0] or "unknown" in check.errors[0]


def test_run_migrate_all_fails_on_registry_error(tmp_path):
    db_path = str(tmp_path / "fail.sqlite")
    _make_group_db(db_path, corrupt=True)
    results = run_migrate_all(db_paths=[db_path], dry_run=False)
    failures = [summary for name, ok, summary in results if name == db_path and not ok]
    assert failures, "corrupt registry records must surface as failures"
