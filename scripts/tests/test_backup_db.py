"""ТЗ 6.5: backup script tests.

Covers:
    - backup_creates_valid_sqlite  — online-backup API produces a readable copy;
    - retention_deletes_old        — only N most recent *.bak remain;
    - dry_run_does_nothing         — plan only: no writes, no deletes.
"""
from __future__ import annotations

import io
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


# --------------------------------------------------- TZ 6.10: restore

def _make_backup(src_db, backup_dir, monkeypatch):
    """Create a .bak next to src_db via the backup path under test."""
    created = backup_database(src_db, backup_dir, keep=3, risk_state_path=None)
    assert created, "fixture failed to produce a backup"
    return created[0]


def test_restore_replaces_db_from_backup(src_db, tmp_path):
    """ТЗ 6.10: restore swaps the live DB for the backup content."""
    from scripts.backup_db import restore_database

    backup_path = _make_backup(src_db, str(tmp_path / "backups"), None)

    # "Damage" the live DB: add a row and a second table.
    conn = sqlite3.connect(src_db)
    conn.execute("INSERT INTO t (v) VALUES ('corruption')")
    conn.execute("CREATE TABLE junk (x INTEGER)")
    conn.commit()
    conn.close()

    restore_database(backup_path, src_db)

    conn = sqlite3.connect(src_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert count == 10                      # back to backup content
    assert "junk" not in tables
    assert integrity == "ok"
    # Pre-restore safety copy exists for manual rollback.
    assert os.path.exists(src_db + ".pre_restore.bak")


def test_restore_refuses_corrupt_backup(src_db, tmp_path):
    from scripts.backup_db import restore_database

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    bad = backup_dir / "corrupt.sqlite.bak"
    bad.write_bytes(b"this is not a sqlite database")

    with pytest.raises(ValueError, match="integrity_check"):
        restore_database(str(bad), src_db)
    # Live DB untouched.
    assert os.path.exists(src_db)


def test_restore_missing_backup_raises(src_db, tmp_path):
    from scripts.backup_db import restore_database

    with pytest.raises(FileNotFoundError):
        restore_database(str(tmp_path / "ghost.bak"), src_db)


def test_restore_copies_risk_state_bak(src_db, tmp_path):
    from scripts.backup_db import restore_database

    backup_path = _make_backup(src_db, str(tmp_path / "backups"), None)
    state = tmp_path / "risk_state.json"
    state_bak = tmp_path / "risk_state.json.bak"
    state.write_text('{"tripped": true}', encoding="utf-8")
    state_bak.write_text('{"tripped": false}', encoding="utf-8")

    restore_database(backup_path, src_db, risk_state_path=str(state))

    import json as _json
    assert _json.loads(state.read_text(encoding="utf-8"))["tripped"] is False


def test_restore_requires_confirmation(src_db, tmp_path, monkeypatch, caplog):
    """ТЗ 6.10: non-interactive --restore without --yes must refuse (exit 2)."""
    from scripts import backup_db as mod

    backup_path = _make_backup(src_db, str(tmp_path / "backups"), None)
    monkeypatch.setattr("sys.stdin", io.StringIO("RESTORE\n"))  # not a tty

    rc = mod.main(["--restore", backup_path, "--db-path", src_db])
    assert rc == 2
    # DB must be untouched.
    conn = sqlite3.connect(src_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 10
    finally:
        conn.close()


def test_restore_with_yes_flag_succeeds(src_db, tmp_path, monkeypatch):
    from scripts import backup_db as mod

    backup_path = _make_backup(src_db, str(tmp_path / "backups"), None)
    conn = sqlite3.connect(src_db)
    conn.execute("INSERT INTO t (v) VALUES ('extra')")
    conn.commit()
    conn.close()

    rc = mod.main(["--restore", backup_path, "--yes", "--db-path", src_db])
    assert rc == 0
    conn = sqlite3.connect(src_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 10
    finally:
        conn.close()


def test_restore_interactive_confirmation_tty_accepts(src_db, tmp_path, monkeypatch):
    from scripts import backup_db as mod

    backup_path = _make_backup(src_db, str(tmp_path / "backups"), None)
    conn = sqlite3.connect(src_db)
    conn.execute("INSERT INTO t (v) VALUES ('extra')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(mod.sys, "stdin", io.StringIO("RESTORE\n"))
    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "RESTORE\n".strip())

    rc = mod.main(["--restore", backup_path, "--db-path", src_db])
    assert rc == 0
    conn = sqlite3.connect(src_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 10
    finally:
        conn.close()


def test_restore_interactive_wrong_answer_aborts(src_db, tmp_path, monkeypatch):
    from scripts import backup_db as mod

    backup_path = _make_backup(src_db, str(tmp_path / "backups"), None)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO("no\n"))
    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "no")

    rc = mod.main(["--restore", backup_path, "--db-path", src_db])
    assert rc == 2
    conn = sqlite3.connect(src_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 10
    finally:
        conn.close()
