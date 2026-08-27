"""Tests for UsJournal Schema Versioning and Migrations (P2-4)."""
import os
import sqlite3
import tempfile
import pytest

from usstocks.journal import CURRENT_SCHEMA_VERSION, UsJournal


def test_schema_migrations_applied_on_creation():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_mig.sqlite")
        journal = UsJournal(db_path)

        assert journal.get_schema_version() == CURRENT_SCHEMA_VERSION

        # Check migrations table
        rows = journal._conn.execute(
            "SELECT version, description FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["version"] == 1
        assert rows[1]["version"] == 2

        # Reopening the database does not re-apply or fail
        journal.close()
        journal2 = UsJournal(db_path)
        assert journal2.get_schema_version() == CURRENT_SCHEMA_VERSION
        journal2.close()
