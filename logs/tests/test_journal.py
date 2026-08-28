"""
Unit tests for logs/journal.py and logs/reporter.py.
Run with: pytest logs/tests/test_journal.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from logs.journal import SignalJournal
from logs.reporter import format_summary_message, generate_summary

SAMPLE_SIGNAL = {
    "bias": "long",
    "confidence": 0.72,
    "entry_zone": [2400.0, 2402.0],
    "invalidation": 2390.0,
    "targets": [2420.0],
    "reasoning_summary": "regime=trend_up, rule_vote=1, ml_p_long=0.72",
    "regime": "trend_up",
    "timestamp_utc": 1700000000,
    "session": "london",
    "generated_at": "2024-01-01T10:00:00+00:00",
}


@pytest.fixture
def tmp_journal(tmp_path):
    return SignalJournal(str(tmp_path / "test_journal.sqlite"))


def test_log_signal_returns_id(tmp_journal):
    row_id = tmp_journal.log_signal(SAMPLE_SIGNAL)
    assert isinstance(row_id, int) and row_id > 0


def test_fetch_all_returns_logged_signal(tmp_journal):
    tmp_journal.log_signal(SAMPLE_SIGNAL)
    rows = tmp_journal.fetch_all()
    assert len(rows) == 1
    assert rows[0][5] == "long"  # bias column


def test_update_outcome_fills_result(tmp_journal):
    row_id = tmp_journal.log_signal(SAMPLE_SIGNAL)
    tmp_journal.update_outcome(row_id, "target", pnl=150.0)
    rows = tmp_journal.fetch_all()
    # trailing columns: ..., outcome, outcome_pnl, outcome_logged_at, asset_key
    assert rows[0][-4] == "target"
    assert rows[0][-3] == 150.0


def test_fetch_unresolved_excludes_resolved(tmp_journal):
    id1 = tmp_journal.log_signal(SAMPLE_SIGNAL)
    tmp_journal.log_signal({**SAMPLE_SIGNAL, "bias": "short"})
    tmp_journal.update_outcome(id1, "stop", pnl=-100.0)
    unresolved = tmp_journal.fetch_unresolved()
    assert len(unresolved) == 1
    assert unresolved[0][5] == "short"


def test_reporter_summary_on_empty_db(tmp_path):
    journal = SignalJournal(str(tmp_path / "empty.sqlite"))
    summary = generate_summary(str(tmp_path / "empty.sqlite"))
    assert summary["n_resolved"] == 0


def test_reporter_summary_correct_win_rate(tmp_path):
    db_path = str(tmp_path / "j.sqlite")
    journal = SignalJournal(db_path)
    id1 = journal.log_signal(SAMPLE_SIGNAL)
    id2 = journal.log_signal({**SAMPLE_SIGNAL, "session": "ny"})
    id3 = journal.log_signal({**SAMPLE_SIGNAL, "session": "asia"})
    journal.update_outcome(id1, "target", pnl=150.0)
    journal.update_outcome(id2, "stop", pnl=-100.0)
    journal.update_outcome(id3, "target", pnl=150.0)
    summary = generate_summary(db_path)
    assert summary["n_resolved"] == 3
    assert abs(summary["win_rate"] - 66.7) < 0.1


def test_format_summary_message_nonempty(tmp_path):
    db_path = str(tmp_path / "j2.sqlite")
    journal = SignalJournal(db_path)
    row_id = journal.log_signal(SAMPLE_SIGNAL)
    journal.update_outcome(row_id, "target", pnl=150.0)
    summary = generate_summary(db_path)
    msg = format_summary_message(summary)
    assert "Performance Report" in msg
    assert "Win rate" in msg
