"""Tests for session_tagger.tag_session_with_weekend and backfill_data._session_label.

Sunday 21:00+ UTC must be tagged as a real session (e.g. newyork),
NOT 'weekend'. Saturday and Sunday before 21:00 UTC remain 'weekend'.
"""
import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.session_tagger import tag_session, tag_session_with_weekend

CFG = load_config()
SESSIONS = CFG["sessions"]


# ---------------------------------------------------------------------------
# tag_session_with_weekend
# ---------------------------------------------------------------------------
class TestTagSessionWithWeekend:
    """Sunday 21:00+ UTC → real session, not 'weekend'."""

    def test_saturday_is_weekend(self):
        """Saturday 12:00 UTC → weekend."""
        ts = pd.Timestamp("2026-03-07 12:00", tz="UTC")  # Saturday
        assert tag_session_with_weekend(ts, SESSIONS) == "weekend"

    def test_sunday_before_21_is_weekend(self):
        """Sunday 20:59 UTC → weekend (market still closed)."""
        ts = pd.Timestamp("2026-03-01 20:59", tz="UTC")  # Sunday
        assert tag_session_with_weekend(ts, SESSIONS) == "weekend"

    def test_sunday_21_is_not_weekend(self):
        """Sunday 21:00 UTC → newyork (FX market reopen)."""
        ts = pd.Timestamp("2026-03-01 21:00", tz="UTC")  # Sunday
        result = tag_session_with_weekend(ts, SESSIONS)
        assert result != "weekend"
        assert "newyork" in result

    def test_sunday_22_is_not_weekend(self):
        """Sunday 22:00 UTC → off_session (newyork ended at 22), NOT weekend."""
        ts = pd.Timestamp("2026-03-01 22:00", tz="UTC")  # Sunday
        result = tag_session_with_weekend(ts, SESSIONS)
        assert result != "weekend"

    def test_sunday_23_is_not_weekend(self):
        """Sunday 23:00 UTC → off_session, NOT weekend."""
        ts = pd.Timestamp("2026-03-01 23:00", tz="UTC")  # Sunday
        result = tag_session_with_weekend(ts, SESSIONS)
        assert result != "weekend"

    def test_monday正常使用(self):
        """Monday 10:00 UTC → london (normal weekday)."""
        ts = pd.Timestamp("2026-03-02 10:00", tz="UTC")  # Monday
        result = tag_session_with_weekend(ts, SESSIONS)
        assert result != "weekend"
        assert "london" in result

    def test_weekday_off_session(self):
        """Weekday 14:00 UTC → newyork (not weekend)."""
        ts = pd.Timestamp("2026-03-02 14:00", tz="UTC")  # Monday
        result = tag_session_with_weekend(ts, SESSIONS)
        assert result != "weekend"

    def test_accepts_epoch_seconds(self):
        """Function accepts epoch seconds (int)."""
        ts = int(dt.datetime(2026, 3, 1, 22, 0, tzinfo=dt.UTC).timestamp())
        result = tag_session_with_weekend(ts, SESSIONS)
        assert result != "weekend"

    def test_accepts_naive_timestamp_assumes_utc(self):
        """Naive timestamp → treated as UTC."""
        ts = pd.Timestamp("2026-03-01 22:00")  # naive, Sunday
        result = tag_session_with_weekend(ts, SESSIONS)
        assert result != "weekend"

    def test_sunday_2059_vs_2100_boundary(self):
        """Exact boundary: 20:59 = weekend, 21:00 = session."""
        before = pd.Timestamp("2026-06-28 20:59", tz="UTC")  # Sunday
        after = pd.Timestamp("2026-06-28 21:00", tz="UTC")   # Sunday
        assert tag_session_with_weekend(before, SESSIONS) == "weekend"
        assert tag_session_with_weekend(after, SESSIONS) != "weekend"


# ---------------------------------------------------------------------------
# Verify tag_session (existing) still works for weekdays
# ---------------------------------------------------------------------------
class TestTagSession:
    """Existing tag_session must not be broken by the new function."""

    def test_weekday_london(self):
        ts = pd.Timestamp("2026-03-02 10:00", tz="UTC")  # Monday
        assert tag_session(ts, SESSIONS) == "london"

    def test_weekday_newyork(self):
        ts = pd.Timestamp("2026-03-02 14:00", tz="UTC")  # Monday
        assert tag_session(ts, SESSIONS) == "newyork"

    def test_weekday_asia(self):
        ts = pd.Timestamp("2026-03-02 04:00", tz="UTC")  # Monday
        assert tag_session(ts, SESSIONS) == "asia"

    def test_off_session(self):
        """Hour 13 = london end (exclusive) + newyork start -> newyork."""
        ts = pd.Timestamp("2026-03-02 13:00", tz="UTC")  # Monday
        result = tag_session(ts, SESSIONS)
        # 13 is not in london (8-13 exclusive end) but is in newyork (13-22)
        assert "newyork" in result


# ---------------------------------------------------------------------------
# _session_label (backfill_data.py)
# ---------------------------------------------------------------------------
def _backfill_session_label(ts: pd.Timestamp) -> str:
    """Import and call the backfill_data._session_label function."""
    from scripts.backfill_data import _session_label
    return _session_label(ts)


class TestBackfillSessionLabel:
    """backfill_data._session_label must match tag_session_with_weekend logic."""

    def test_saturday_is_weekend(self):
        ts = pd.Timestamp("2026-03-07 12:00", tz="UTC")  # Saturday
        assert _backfill_session_label(ts) == "weekend"

    def test_sunday_before_21_is_weekend(self):
        ts = pd.Timestamp("2026-03-01 20:59", tz="UTC")  # Sunday
        assert _backfill_session_label(ts) == "weekend"

    def test_sunday_21_is_newyork(self):
        ts = pd.Timestamp("2026-03-01 21:00", tz="UTC")  # Sunday
        assert _backfill_session_label(ts) == "newyork"

    def test_sunday_22_is_not_weekend(self):
        ts = pd.Timestamp("2026-03-01 22:00", tz="UTC")  # Sunday
        assert _backfill_session_label(ts) != "weekend"

    def test_sunday_23_is_not_weekend(self):
        ts = pd.Timestamp("2026-03-01 23:00", tz="UTC")  # Sunday
        assert _backfill_session_label(ts) != "weekend"

    def test_monday_london(self):
        ts = pd.Timestamp("2026-03-02 10:00", tz="UTC")  # Monday
        assert _backfill_session_label(ts) == "london"

    def test_monday_asia(self):
        ts = pd.Timestamp("2026-03-02 04:00", tz="UTC")  # Monday
        assert _backfill_session_label(ts) == "asia"

    def test_monday_newyork(self):
        ts = pd.Timestamp("2026-03-02 14:00", tz="UTC")  # Monday
        assert _backfill_session_label(ts) == "newyork"

    def test_boundary_sunday_2059_vs_2100(self):
        before = pd.Timestamp("2026-06-28 20:59", tz="UTC")  # Sunday
        after = pd.Timestamp("2026-06-28 21:00", tz="UTC")   # Sunday
        assert _backfill_session_label(before) == "weekend"
        assert _backfill_session_label(after) == "newyork"
