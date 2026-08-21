# -*- coding: utf-8 -*-
"""Tests for news.calendar_feed and news.guard modules."""
import datetime as dt
import json
import os
import tempfile
import time
from unittest.mock import patch, MagicMock

import pytest

from news.calendar_feed import CalendarFeed, CalendarEvent, _parse_event
from news.guard import NewsGuard, ASSET_CURRENCIES


# ---------------------------------------------------------------------------
# CalendarEvent dataclass
# ---------------------------------------------------------------------------

class TestCalendarEvent:
    def test_is_high(self):
        ev = CalendarEvent("CPI", "USD", dt.datetime(2026, 8, 18, 12, 30), "High")
        assert ev.is_high is True
        assert ev.is_medium_or_high is True

    def test_is_medium(self):
        ev = CalendarEvent("PMI", "EUR", dt.datetime(2026, 8, 18, 8, 0), "Medium")
        assert ev.is_high is False
        assert ev.is_medium_or_high is True

    def test_is_low(self):
        ev = CalendarEvent("Foo", "GBP", dt.datetime(2026, 8, 18, 6, 0), "Low")
        assert ev.is_high is False
        assert ev.is_medium_or_high is False


# ---------------------------------------------------------------------------
# _parse_event
# ---------------------------------------------------------------------------

class TestParseEvent:
    def test_valid_event(self):
        raw = {
            "title": "Core CPI m/m",
            "country": "USD",
            "date": "2026-08-18T08:30:00-04:00",
            "impact": "High",
            "forecast": "0.3%",
            "previous": "0.2%",
        }
        ev = _parse_event(raw)
        assert ev is not None
        assert ev.title == "Core CPI m/m"
        assert ev.currency == "USD"
        assert ev.impact == "High"
        assert ev.datetime_utc == dt.datetime(2026, 8, 18, 12, 30)  # UTC

    def test_missing_title(self):
        raw = {"country": "USD", "date": "2026-08-18T08:30:00-04:00", "impact": "High"}
        assert _parse_event(raw) is None

    def test_missing_date(self):
        raw = {"title": "CPI", "country": "USD", "impact": "High"}
        assert _parse_event(raw) is None

    def test_invalid_date(self):
        raw = {"title": "CPI", "country": "USD", "date": "not-a-date", "impact": "High"}
        assert _parse_event(raw) is None


# ---------------------------------------------------------------------------
# CalendarFeed (unit tests with mocked API)
# ---------------------------------------------------------------------------

class TestCalendarFeed:
    """Unit tests with mocked API — no real HTTP calls."""
    _MOCK_EVENTS = [
        {
            "title": "Core CPI m/m",
            "country": "USD",
            "date": "2026-08-18T08:30:00-04:00",
            "impact": "High",
            "forecast": "0.3%",
            "previous": "0.2%",
        },
        {
            "title": "FOMC Statement",
            "country": "USD",
            "date": "2026-08-18T14:00:00-04:00",
            "impact": "High",
            "forecast": "",
            "previous": "",
        },
        {
            "title": "ECB Press Conference",
            "country": "EUR",
            "date": "2026-08-19T07:30:00+00:00",
            "impact": "Medium",
            "forecast": "",
            "previous": "",
        },
        {
            "title": "Unemployment Rate",
            "country": "USD",
            "date": "2026-08-18T08:30:00-04:00",
            "impact": "Low",
            "forecast": "4.0%",
            "previous": "4.1%",
        },
    ]

    def _make_events(self) -> list[dict]:
        """Return a mock ForexFactory API response."""
        return [
            {
                "title": "Core CPI m/m",
                "country": "USD",
                "date": "2026-08-18T08:30:00-04:00",
                "impact": "High",
                "forecast": "0.3%",
                "previous": "0.2%",
            },
            {
                "title": "FOMC Statement",
                "country": "USD",
                "date": "2026-08-18T14:00:00-04:00",
                "impact": "High",
                "forecast": "",
                "previous": "",
            },
            {
                "title": "ECB Press Conference",
                "country": "EUR",
                "date": "2026-08-19T07:30:00+00:00",
                "impact": "Medium",
                "forecast": "",
                "previous": "",
            },
            {
                "title": "Unemployment Rate",
                "country": "USD",
                "date": "2026-08-18T08:30:00-04:00",
                "impact": "Low",
                "forecast": "4.0%",
                "previous": "4.1%",
            },
        ]

    @patch("news.calendar_feed.urllib.request.urlopen")
    def test_fetch_week(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(self._make_events()).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        feed = CalendarFeed()
        feed._cache_ts = 0  # force refresh
        events = feed._fetch_week("thisweek")
        assert len(events) == 4
        assert sum(e.is_high for e in events) == 2

    def test_get_high_impact(self):
        feed = CalendarFeed()
        feed._cache = [_parse_event(e) for e in self._MOCK_EVENTS if _parse_event(e)]
        feed._cache_ts = time.time()
        # Use reference time before the events (2026-08-18 10:00 UTC)
        ref = dt.datetime(2026, 8, 18, 10, 0)
        high = feed.get_high_impact(hours=48, reference=ref)
        assert len(high) == 2
        assert all(e.is_high for e in high)

    @patch("news.calendar_feed.urllib.request.urlopen")
    def test_is_red_zone_within_buffer(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(self._make_events()).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        feed = CalendarFeed()
        feed._cache_ts = 0
        feed._refresh()

        # Core CPI at 12:30 UTC; check at 12:15 UTC (15 min before)
        now = dt.datetime(2026, 8, 18, 12, 15)
        assert feed.is_red_zone(now, buffer_min=30) is True

        # Check at 12:00 UTC (30 min before — edge)
        now2 = dt.datetime(2026, 8, 18, 12, 0)
        assert feed.is_red_zone(now2, buffer_min=30) is True

        # Check at 11:59 UTC (31 min before — outside buffer)
        now3 = dt.datetime(2026, 8, 18, 11, 59)
        assert feed.is_red_zone(now3, buffer_min=30) is False

    @patch("news.calendar_feed.urllib.request.urlopen")
    def test_is_red_zone_outside_buffer(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(self._make_events()).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        feed = CalendarFeed()
        feed._cache_ts = 0
        feed._refresh()

        # 10:00 UTC — 2.5h before CPI
        now = dt.datetime(2026, 8, 18, 10, 0)
        assert feed.is_red_zone(now, buffer_min=30) is False

    @patch("news.calendar_feed.urllib.request.urlopen")
    def test_is_red_zone_currency_filter(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(self._make_events()).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        feed = CalendarFeed()
        feed._cache_ts = 0
        feed._refresh()

        # Core CPI at 12:30 UTC — 15 min before, but filtering for EUR only
        now = dt.datetime(2026, 8, 18, 12, 15)
        assert feed.is_red_zone(now, buffer_min=30, currencies={"EUR"}) is False

    def test_next_high_impact(self):
        feed = CalendarFeed()
        feed._cache = [_parse_event(e) for e in self._MOCK_EVENTS if _parse_event(e)]
        feed._cache_ts = time.time()

        # Next HIGH after 10:00 UTC
        now = dt.datetime(2026, 8, 18, 10, 0)
        nxt = feed.next_high_impact(reference=now)
        assert nxt is not None
        assert nxt.title == "Core CPI m/m"
        assert nxt.datetime_utc == dt.datetime(2026, 8, 18, 12, 30)

    def test_format_upcoming(self):
        feed = CalendarFeed()
        feed._cache = [_parse_event(e) for e in self._MOCK_EVENTS if _parse_event(e)]
        feed._cache_ts = time.time()
        # Use reference time before the events so they appear as "upcoming"
        ref = dt.datetime(2026, 8, 18, 10, 0)
        text = feed.format_upcoming(hours=48, reference=ref)
        assert "Core CPI" in text
        assert "FOMC" in text
        assert "USD" in text

    def test_cache_disk_roundtrip(self, tmp_path):
        """Test that disk cache saves and loads correctly."""
        cache_path = str(tmp_path / "cache.json")

        # Create feed with events
        feed1 = CalendarFeed()
        feed1._disk_cache_path = cache_path
        feed1._cache = [
            CalendarEvent("CPI", "USD", dt.datetime(2026, 8, 18, 12, 30), "High"),
            CalendarEvent("PMI", "EUR", dt.datetime(2026, 8, 18, 14, 0), "Medium"),
        ]
        feed1._cache_ts = time.time()
        feed1._save_disk_cache()

        # Create new feed — should load from disk
        feed2 = CalendarFeed()
        feed2._disk_cache_path = cache_path
        feed2._load_disk_cache()
        assert len(feed2._cache) == 2
        assert feed2._cache[0].title == "CPI"
        assert feed2._cache[1].impact == "Medium"


# ---------------------------------------------------------------------------
# NewsGuard (unit tests)
# ---------------------------------------------------------------------------

class TestNewsGuard:
    def _make_guard_with_events(self) -> tuple[NewsGuard, list[CalendarEvent]]:
        """Create a guard with mock events injected."""
        events = [
            CalendarEvent("NFP", "USD", dt.datetime(2026, 8, 20, 12, 30), "High"),
            CalendarEvent("CPI", "USD", dt.datetime(2026, 8, 21, 12, 30), "High"),
            CalendarEvent("PMI", "EUR", dt.datetime(2026, 8, 21, 8, 0), "Medium"),
            CalendarEvent("Retail Sales", "GBP", dt.datetime(2026, 8, 22, 6, 0), "Low"),
        ]
        guard = NewsGuard(enabled=True, buffer_before_min=30, buffer_after_min=30)
        guard._feed._cache = events
        guard._feed._cache_ts = time.time()
        return guard, events

    def test_disabled_guard_never_blocks(self):
        guard = NewsGuard(enabled=False)
        assert guard.is_blocked() is False

    def test_blocks_within_buffer(self):
        guard, _ = self._make_guard_with_events()
        # NFP at 12:30 UTC on Aug 20; check at 12:15 UTC
        now = dt.datetime(2026, 8, 20, 12, 15)
        assert guard.is_blocked(now) is True

    def test_blocks_after_event(self):
        guard, _ = self._make_guard_with_events()
        # NFP at 12:30 UTC on Aug 20; check at 12:45 UTC (15 min after)
        now = dt.datetime(2026, 8, 20, 12, 45)
        assert guard.is_blocked(now) is True

    def test_clear_outside_buffer(self):
        guard, _ = self._make_guard_with_events()
        # 10:00 UTC — 2.5h before NFP
        now = dt.datetime(2026, 8, 20, 10, 0)
        assert guard.is_blocked(now) is False

    def test_currency_filter(self):
        guard, _ = self._make_guard_with_events()
        # NFP at 12:30 UTC on Aug 20 — 15 min before
        # EURUSD maps to {EUR, USD}, so NFP (USD) IS in the filter
        # Use a currency NOT in the asset mapping to truly filter out
        # Test: GBPUSD maps to {GBP, USD} — NFP (USD) matches
        # Test: a hypothetical EUR-only asset would NOT match NFP (USD)
        now = dt.datetime(2026, 8, 20, 12, 15)
        # For XAUUSD: currencies={"USD"}, NFP is USD -> blocks
        assert guard.is_blocked(now, asset_key="XAUUSD") is True
        # For a pure-EUR asset (not in ASSET_CURRENCIES, so currencies=None)
        # -> checks ALL events -> blocks (NFP is near)
        # Instead, test that passing explicit currencies filters correctly
        guard._feed._cache = [
            CalendarEvent("NFP", "USD", dt.datetime(2026, 8, 20, 12, 30), "High"),
            CalendarEvent("CPI", "EUR", dt.datetime(2026, 8, 20, 12, 30), "High"),
        ]
        # Only EUR events should block for EURUSD-like filter
        result_eur = guard._feed.is_red_zone(now, buffer_min=30, currencies={"EUR"})
        result_usd = guard._feed.is_red_zone(now, buffer_min=30, currencies={"USD"})
        assert result_eur is True   # CPI EUR is in buffer
        assert result_usd is True   # NFP USD is in buffer
        # Filter for GBP only — neither matches
        result_gbp = guard._feed.is_red_zone(now, buffer_min=30, currencies={"GBP"})
        assert result_gbp is False

    def test_blocks_for_correct_currency(self):
        guard, _ = self._make_guard_with_events()
        # NFP at 12:30 UTC — 15 min before, XAUUSD checks USD events
        now = dt.datetime(2026, 8, 20, 12, 15)
        assert guard.is_blocked(now, asset_key="XAUUSD") is True

    def test_next_event(self):
        guard, _ = self._make_guard_with_events()
        # Use a reference time before all events
        ref = dt.datetime(2026, 8, 19, 10, 0)
        nxt = guard._feed.next_high_impact(reference=ref)
        assert nxt is not None
        assert nxt.title == "NFP"

    def test_next_event_currency_filter(self):
        guard, _ = self._make_guard_with_events()
        # Filter for EUR — next EUR HIGH event
        ref = dt.datetime(2026, 8, 19, 10, 0)
        nxt = guard._feed.next_high_impact(reference=ref, currencies={"EUR"})
        # No EUR HIGH events in test data (only EUR Medium)
        assert nxt is None

    def test_status_text_enabled(self):
        guard, _ = self._make_guard_with_events()
        status = guard.status_text()
        assert "News Guard" in status
        assert "buffer" in status.lower()

    def test_status_text_disabled(self):
        guard = NewsGuard(enabled=False)
        status = guard.status_text()
        assert "DISABLED" in status

    def test_from_config(self):
        cfg = {
            "ensemble": {
                "use_news_guard": True,
                "news_buffer_before_min": 45,
                "news_buffer_after_min": 15,
                "news_feed_failure_policy_live": "fail_open",
            }
        }
        guard = NewsGuard.from_config(cfg)
        assert guard.enabled is True
        assert guard.buffer_before_min == 45
        assert guard.buffer_after_min == 15
        assert guard.failure_policy == "fail_open"

    def test_historical_csv_loading(self, tmp_path):
        csv_path = str(tmp_path / "calendar.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("timestamp_utc,title,country,impact\n")
            f.write("2026-08-20T12:30:00,Core CPI,USD,High\n")
            f.write("2026-08-20T14:00:00,FOMC,USD,High\n")

        guard = NewsGuard(enabled=True, historical_csv_path=csv_path)
        assert len(guard._historical_events) == 2
        assert guard._historical_events[0].title == "Core CPI"

    def test_historical_blocks_in_range(self, tmp_path):
        csv_path = str(tmp_path / "calendar.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("timestamp_utc,title,country,impact\n")
            f.write("2026-08-20T12:30:00,Core CPI,USD,High\n")

        guard = NewsGuard(enabled=True, historical_csv_path=csv_path,
                          buffer_before_min=30, buffer_after_min=30)
        # 15 min before CPI
        now = dt.datetime(2026, 8, 20, 12, 15)
        assert guard.is_blocked(now) is True

    def test_asset_currencies_mapping(self):
        assert "USD" in ASSET_CURRENCIES["XAUUSD"]
        assert "EUR" in ASSET_CURRENCIES["EURUSD"]
        assert "GBP" in ASSET_CURRENCIES["GBPUSD"]
        assert "USD" in ASSET_CURRENCIES["BTCUSD"]
