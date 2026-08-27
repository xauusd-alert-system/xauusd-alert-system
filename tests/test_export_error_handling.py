"""Tests for Journal Export error handling and atomic file writes (P1-8)."""
import os
import tempfile
from unittest.mock import patch
import pytest

from usstocks.journal import JournalExportError, UsJournal


def test_export_day_csv_atomic_write():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "j.sqlite")
        export_dir = os.path.join(td, "exports")
        journal = UsJournal(db_path)
        journal.ensure_session("2026-08-27")

        path = journal.export_day_csv("2026-08-27", export_dir)
        assert os.path.exists(path)
        # Verify no temp files left behind
        files = os.listdir(export_dir)
        assert len(files) == 1
        assert files[0] == "us_signals_2026-08-27.csv"
        journal.close()


def test_export_day_csv_raises_on_invalid_input():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "j.sqlite")
        journal = UsJournal(db_path)

        with pytest.raises(JournalExportError):
            journal.export_day_csv("", td)

        with pytest.raises(JournalExportError):
            journal.export_day_csv(None, td)  # type: ignore

        journal.close()


def test_export_day_csv_cleans_up_on_failure():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "j.sqlite")
        journal = UsJournal(db_path)

        # Mock os.replace to fail, verifying tmp file cleanup
        with patch("os.replace", side_effect=OSError("Disk write error")):
            with pytest.raises(JournalExportError, match="Failed to export CSV"):
                journal.export_day_csv("2026-08-27", td)

        # Verify no orphan files remain
        remaining = [f for f in os.listdir(td) if f.startswith("us_signals_")]
        assert len(remaining) == 0
        journal.close()
