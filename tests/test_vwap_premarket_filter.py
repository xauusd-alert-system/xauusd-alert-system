"""Tests for VWAP premarket filtering (P0-6)."""
from datetime import datetime, time
from zoneinfo import ZoneInfo
import pytest

from usstocks.indicators import (
    NY,
    filter_regular_session,
    is_regular_session,
    opening_range,
    session_vwap_series,
)
from usstocks.models import Bar


def test_vwap_ignores_premarket():
    # Premarket bars with massive volume at price 200
    pm1 = Bar(datetime(2026, 8, 27, 8, 30, tzinfo=NY), 200, 200, 200, 200, 1_000_000)
    pm2 = Bar(datetime(2026, 8, 27, 9, 0, tzinfo=NY), 200, 200, 200, 200, 1_000_000)
    pm3 = Bar(datetime(2026, 8, 27, 9, 25, tzinfo=NY), 200, 200, 200, 200, 1_000_000)

    # Regular session bars at price 100 with small volume
    reg1 = Bar(datetime(2026, 8, 27, 9, 30, tzinfo=NY), 100, 100, 100, 100, 100)
    reg2 = Bar(datetime(2026, 8, 27, 9, 35, tzinfo=NY), 102, 102, 102, 102, 100)

    series = session_vwap_series([pm1, pm2, pm3, reg1, reg2])
    assert len(series) == 5
    # First regular session bar VWAP should be 100.0, NOT polluted by 200.0 premarket volume
    assert series[3] == pytest.approx(100.0)
    # Second regular session bar VWAP should be (100*100 + 102*100)/200 = 101.0
    assert series[4] == pytest.approx(101.0)


def test_opening_range_ignores_premarket():
    # Premarket bars
    pm1 = Bar(datetime(2026, 8, 27, 8, 30, tzinfo=NY), 50, 60, 40, 55, 500)
    pm2 = Bar(datetime(2026, 8, 27, 9, 0, tzinfo=NY), 55, 65, 50, 60, 500)

    # First 3 regular session 5m bars
    r1 = Bar(datetime(2026, 8, 27, 9, 30, tzinfo=NY), 100, 105, 99, 102, 1000)
    r2 = Bar(datetime(2026, 8, 27, 9, 35, tzinfo=NY), 102, 108, 101, 107, 1000)
    r3 = Bar(datetime(2026, 8, 27, 9, 40, tzinfo=NY), 107, 110, 105, 106, 1000)

    rng = opening_range([pm1, pm2, r1, r2, r3], range_minutes=15)
    assert rng is not None
    # Opening range high should be max(105, 108, 110) = 110, low min(99, 101, 105) = 99
    assert rng == (110, 99)


def test_regular_session_helpers():
    pm = Bar(datetime(2026, 8, 27, 9, 15, tzinfo=NY), 100, 101, 99, 100, 100)
    reg = Bar(datetime(2026, 8, 27, 9, 30, tzinfo=NY), 100, 101, 99, 100, 100)
    ah = Bar(datetime(2026, 8, 27, 16, 5, tzinfo=NY), 100, 101, 99, 100, 100)

    assert not is_regular_session(pm)
    assert is_regular_session(reg)
    assert not is_regular_session(ah)

    filtered = filter_regular_session([pm, reg, ah])
    assert len(filtered) == 1
    assert filtered[0].ts == reg.ts
