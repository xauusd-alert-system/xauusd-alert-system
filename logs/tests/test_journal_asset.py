"""Tests for the signal_journal asset_key migration (Wave-0 MQL5 plan)."""

from __future__ import annotations

import sqlite3

from logs.journal import SignalJournal


def test_legacy_journal_migrates_non_destructively(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    # create a journal with the OLD schema (no asset_key)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE signal_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generated_at TEXT NOT NULL,
            timestamp_utc INTEGER NOT NULL,
            session TEXT, regime TEXT, bias TEXT NOT NULL,
            confidence REAL NOT NULL, entry_zone_low REAL, entry_zone_high REAL,
            invalidation REAL, target REAL, reasoning TEXT,
            outcome TEXT, outcome_pnl REAL, outcome_logged_at TEXT
        )
    """)
    conn.execute("""
        INSERT INTO signal_journal (generated_at, timestamp_utc, bias, confidence)
        VALUES ('2026-01-01T00:00:00Z', 1767225600, 'long', 0.7)
    """)
    conn.commit()
    conn.close()

    journal = SignalJournal(path)
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(signal_journal)")}
    assert "asset_key" in columns

    # legacy row survives with NULL asset
    rows = journal.fetch_all()
    assert len(rows) == 1
    assert rows[0][15] is None


def test_log_signal_persists_asset_key(tmp_path):
    path = str(tmp_path / "journal.sqlite")
    journal = SignalJournal(path)
    row_id = journal.log_signal(
        {
            "generated_at": "2026-01-01T00:00:00Z",
            "timestamp_utc": 1767225600,
            "session": "london",
            "regime": "trend_up",
            "bias": "long",
            "confidence": 0.71,
        },
        asset_key="XAUUSD",
    )
    assert row_id == 1
    rows = journal.fetch_all()
    assert rows[0][15] == "XAUUSD"


def test_log_signal_asset_from_signal_dict(tmp_path):
    path = str(tmp_path / "journal2.sqlite")
    journal = SignalJournal(path)
    journal.log_signal(
        {
            "timestamp_utc": 1767225600,
            "bias": "short",
            "confidence": 0.6,
            "asset_key": "GBPUSD",
        }
    )
    assert journal.fetch_all()[0][15] == "GBPUSD"
