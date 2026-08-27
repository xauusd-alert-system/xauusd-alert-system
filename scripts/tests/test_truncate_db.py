"""P2-30: truncate_db dry-run and confirmation guards.

The research truncation script deletes data — these tests pin the safety
contract: --dry-run previews counts, deletion without consent is refused,
and --yes --no-confirm actually deletes.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "research"
sys.path.insert(0, str(SCRIPTS_DIR))

import truncate_db  # noqa: E402  (path set above)


@pytest.fixture
def research_db(tmp_path):
    """SQLite DB with two ohlcv_* tables: rows before/after the cutoff."""
    db = tmp_path / "research.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ohlcv_xauusd_m5 (timestamp_utc INTEGER, close REAL)")
    con.execute("CREATE TABLE ohlcv_xauusd_m15 (timestamp_utc INTEGER, close REAL)")
    con.execute("CREATE TABLE other_table (timestamp_utc INTEGER)")  # not ohlcv_*
    # rows at/after the 2026-08-08 UTC cutoff epoch get deleted
    cut = truncate_db.cutoff_epoch("2026-08-08")
    rows_m5 = [(1_000, 1.0), (cut, 2.0), (cut + 90_000, 3.0)]
    rows_m15 = [(2_000, 1.0)]
    con.executemany("INSERT INTO ohlcv_xauusd_m5 VALUES (?, ?)", rows_m5)
    con.executemany("INSERT INTO ohlcv_xauusd_m15 VALUES (?, ?)", rows_m15)
    con.commit()
    con.close()
    return db


def _table_rows(db):
    con = sqlite3.connect(db)
    try:
        return {
            t: con.execute(f"SELECT count(*) FROM \"{t}\"").fetchone()[0]
            for t in ("ohlcv_xauusd_m5", "ohlcv_xauusd_m15", "other_table")
        }
    finally:
        con.close()


def test_truncate_db_dry_run(research_db, capsys):
    """--dry-run prints per-table counts and deletes NOTHING."""
    rc = truncate_db.main(
        ["--db", str(research_db), "--cutoff", "2026-08-08", "--dry-run"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would delete        2 rows from ohlcv_xauusd_m5" in out
    assert "would delete        0 rows from ohlcv_xauusd_m15" in out
    assert "TOTAL rows that would be deleted: 2" in out
    # nothing deleted
    assert _table_rows(research_db) == {
        "ohlcv_xauusd_m5": 3,
        "ohlcv_xauusd_m15": 1,
        "other_table": 0,
    }


def test_truncate_db_requires_confirmation(research_db, capsys):
    """Deletion without --yes is refused (exit code 2), DB untouched."""
    rc = truncate_db.main(["--db", str(research_db), "--cutoff", "2026-08-08"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Refusing to delete" in out
    assert _table_rows(research_db) == {
        "ohlcv_xauusd_m5": 3,
        "ohlcv_xauusd_m15": 1,
        "other_table": 0,
    }


def test_truncate_db_requires_yes_for_non_interactive(research_db):
    """--no-confirm without --yes must not delete (guard against a lone flag)."""
    rc = truncate_db.main(
        ["--db", str(research_db), "--cutoff", "2026-08-08", "--no-confirm"]
    )
    assert rc == 2
    assert _table_rows(research_db)["ohlcv_xauusd_m5"] == 3


def test_truncate_db_deletes_with_yes(research_db, capsys, monkeypatch):
    """--yes with a simulated interactive confirmation deletes only ohlcv_* rows
    at/after the cutoff; other_table is untouched."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    rc = truncate_db.main(["--db", str(research_db), "--cutoff", "2026-08-08", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Are you sure?" in out or "truncated" in out
    rows = _table_rows(research_db)
    # rows before cutoff survive, rows at/after cutoff deleted
    assert rows["ohlcv_xauusd_m5"] == 1
    assert rows["ohlcv_xauusd_m15"] == 1
    assert rows["other_table"] == 0


def test_truncate_db_deletes_with_yes_no_confirm(research_db, monkeypatch):
    """Non-interactive: --yes --no-confirm deletes without prompting."""
    prompted = False

    def _no_prompt(_prompt):
        nonlocal prompted
        prompted = True
        return "n"  # if a prompt ever happens, answer "no"

    monkeypatch.setattr("builtins.input", _no_prompt)
    rc = truncate_db.main(
        ["--db", str(research_db), "--cutoff", "2026-08-08", "--yes", "--no-confirm"]
    )
    assert rc == 0
    assert not prompted
    assert _table_rows(research_db)["ohlcv_xauusd_m5"] == 1


def test_truncate_db_interactive_abort(research_db, capsys, monkeypatch):
    """Answering anything but y/yes at the [y/N] prompt aborts cleanly."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "N")
    rc = truncate_db.main(["--db", str(research_db), "--cutoff", "2026-08-08", "--yes"])
    assert rc == 1
    assert "Aborted" in capsys.readouterr().out
    assert _table_rows(research_db)["ohlcv_xauusd_m5"] == 3
