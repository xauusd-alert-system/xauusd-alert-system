"""Tests for Bar timezone validation (P0-2)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest

from usstocks.models import Bar


def test_bar_rejects_naive_datetime():
    naive_dt = datetime(2026, 8, 27, 9, 30, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar(ts=naive_dt, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000)


def test_bar_accepts_aware_datetime():
    ny_dt = datetime(2026, 8, 27, 9, 30, 0, tzinfo=ZoneInfo("America/New_York"))
    bar = Bar(ts=ny_dt, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000)
    assert bar.ts == ny_dt
    assert bar.typical_price == pytest.approx((101.0 + 99.0 + 100.5) / 3.0)

    utc_dt = datetime(2026, 8, 27, 13, 30, 0, tzinfo=timezone.utc)
    bar_utc = Bar(ts=utc_dt, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000)
    assert bar_utc.ts == utc_dt


def test_bar_rejects_negative_prices_and_volume():
    ny_dt = datetime(2026, 8, 27, 9, 30, 0, tzinfo=ZoneInfo("America/New_York"))
    with pytest.raises(ValueError, match="non-negative"):
        Bar(ts=ny_dt, open=-1.0, high=101.0, low=99.0, close=100.5, volume=1000)
    with pytest.raises(ValueError, match="non-negative"):
        Bar(ts=ny_dt, open=100.0, high=101.0, low=99.0, close=100.5, volume=-5)
