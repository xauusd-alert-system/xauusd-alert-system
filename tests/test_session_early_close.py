"""Tests for Early Close Days session math (P0-7)."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
import pytest

from usstocks.session import NySession, session_from_cfg, NY


def test_early_close_detection_and_closing_time():
    sess = NySession(
        holidays=["2026-12-25"],
        early_closes={"2026-12-24": "13:00", "2026-11-27": "13:00"}
    )
    d_early = date(2026, 12, 24)
    d_normal = date(2026, 12, 23)

    assert sess.is_trading_day(d_early)
    assert sess.is_early_close(d_early)
    assert not sess.is_early_close(d_normal)

    close_early = sess.session_close(d_early)
    assert close_early.time() == time(13, 0)
    assert close_early.tzinfo == NY

    close_normal = sess.session_close(d_normal)
    assert close_normal.time() == time(16, 0)


def test_early_close_minutes_to_close():
    sess = NySession(
        holidays=[],
        early_closes={"2026-11-27": "13:00"}
    )
    # At 12:30 on early close day -> 30 minutes to close
    now = datetime(2026, 11, 27, 12, 30, tzinfo=NY)
    mins = sess.minutes_to_close(now)
    assert mins == pytest.approx(30.0)

    # At 12:30 on regular day -> 210 minutes to close (16:00 - 12:30 = 3.5h = 210m)
    now_reg = datetime(2026, 11, 25, 12, 30, tzinfo=NY)
    mins_reg = sess.minutes_to_close(now_reg)
    assert mins_reg == pytest.approx(210.0)


def test_session_from_cfg_loads_early_closes():
    cfg = {
        "session": {
            "regular_open": "09:30",
            "regular_close": "16:00",
            "holidays": ["2026-01-01"],
            "early_closes": {
                "2026-11-27": "13:00"
            }
        }
    }
    sess = session_from_cfg(cfg)
    assert sess.is_early_close(date(2026, 11, 27))
    assert sess.session_close(date(2026, 11, 27)).time() == time(13, 0)
