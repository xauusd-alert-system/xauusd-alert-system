"""ТЗ 6.5: backup script tests.

Covers:
    - backup_creates_valid_sqlite  — online-backup API produces a readable copy;
    - retention_deletes_old        — only N most recent *.bak remain;
    - dry_run_does_nothing         — plan only: no writes, no deletes.
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from scripts.backup_db import backup_database, prune_backups, validate_backup


@pytest.fixture
def src_db(tmp_path):
    db_path = str(tmp_path / "src.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(10)])
    conn.commit()
    conn.close()
    return db_path


# ------------------------------------------------------ backup_creates_valid

def test_backup_creates_valid_sqlite(src_db, tmp_path):
    backup_dir = str(tmp_path / "backups")
    created = backup_database(src_db, backup_dir, keep=3, risk_state_path=None)

    assert len(created) == 1
    target = created[0]
    assert target.endswith(".bak")

    # A valid, readable SQLite copy containing the source rows.
    assert validate_backup(target)
    conn = sqlite3.connect(target)
    try:
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()
    assert count == 10


def test_backup_copies_risk_state(src_db, tmp_path):
    state = tmp_path / "risk_state.json"
    state.write_text('{"circuit_breaker_tripped": false}', encoding="utf-8")
    backup_dir = str(tmp_path / "backups")

    created = backup_database(src_db, backup_dir, keep=2,
                              risk_state_path=str(state))

    assert len(created) == 2
    import json as _json

    with open(created[1], encoding="utf-8") as f:
        restored = _json.load(f)
    assert restored["circuit_breaker_tripped"] is False


def test_backup_missing_db_is_noop(src_db, tmp_path):
    backup_dir = str(tmp_path / "backups")
    created = backup_database(str(tmp_path / "ghost.sqlite"), backup_dir,
                              keep=3, risk_state_path=None)
    assert created == []


# -------------------------------------------------------- retention_deletes_old

def test_retention_deletes_old(src_db, tmp_path):
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    # Simulate 5 pre-existing backups with distinct mtimes.
    for i in range(5):
        p = os.path.join(backup_dir, f"old{i}.bak")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        os.utime(p, (time.time() - (100 - i), time.time() - (100 - i)))

    backup_database(src_db, backup_dir, keep=2, risk_state_path=None)

    remaining = sorted(f for f in os.listdir(backup_dir) if f.endswith(".bak"))
    assert len(remaining) == 2
    # The two NEWEST survive (old4, old3) plus the fresh DB backup... keep=2
    # means exactly the 2 most recent mtime files overall.
    assert "old4.bak" in remaining


def test_prune_backups_no_dir_is_safe(tmp_path):
    assert prune_backups(str(tmp_path / "absent"), keep=3) == []


# -------------------------------------------------------- dry_run_does_nothing

def test_dry_run_does_nothing(src_db, tmp_path, caplog):
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    pre_existing = os.path.join(backup_dir, "old.bak")
    with open(pre_existing, "w", encoding="utf-8") as f:
        f.write("x")

    created = backup_database(src_db, backup_dir, keep=1, dry_run=True,
                              risk_state_path=None)

    # Nothing created, nothing deleted.
    assert created == []
    assert os.listdir(backup_dir) == ["old.bak"]
    assert not any(f.endswith(".bak") for f in os.listdir(tmp_path))
