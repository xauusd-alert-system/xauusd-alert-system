"""Canonical session label used by ohlcv storage (backfills, DB rebuilds).

Semantics: weekends (UTC) are always ``weekend``; every other bar gets the
config-window label — ``off_session`` outside all windows (e.g. 22:00-23:59 UTC
is NOT newyork, matching config newyork end=22).
"""
from __future__ import annotations

import pytest

from data.session_tagger import tag_session_with_weekend

SESSIONS = {
    "asia": {"start": 0, "end": 8},
    "london": {"start": 8, "end": 13},
    "newyork": {"start": 13, "end": 22},
}


@pytest.mark.parametrize("iso,expected", [
    # weekdays (2026-08-26 is a Wednesday)
    ("2026-08-26 03:30:00+00:00", "asia"),
    ("2026-08-26 10:00:00+00:00", "london"),
    ("2026-08-26 13:00:00+00:00", "newyork"),
    ("2026-08-26 21:59:00+00:00", "newyork"),
    # hours 22-23 are OUTSIDE newyork (config end=22) -> off_session
    ("2026-08-26 22:00:00+00:00", "off_session"),
    ("2026-08-26 23:45:00+00:00", "off_session"),
    # weekends are never asia/london/newyork
    ("2026-08-29 12:00:00+00:00", "weekend"),  # Saturday noon would be london
    ("2026-08-30 15:00:00+00:00", "weekend"),  # Sunday afternoon would be newyork
])
def test_tag_session_with_weekend(iso: str, expected: str) -> None:
    import pandas as pd
    ts = pd.Timestamp(iso)
    assert tag_session_with_weekend(ts, SESSIONS) == expected


def test_accepts_epoch_seconds() -> None:
    import datetime
    ts = datetime.datetime(2026, 8, 26, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
    assert tag_session_with_weekend(ts, SESSIONS) == "london"
