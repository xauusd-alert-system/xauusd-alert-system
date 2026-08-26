"""Tests for HumanizedTimer — delays, news-aware, earnings, min gap."""
import pytest
from datetime import datetime, date, timedelta

from challenge.stealth.humanized_timer import HumanizedTimer


def _dt(y=2026, mo=8, d=25, h=10, mi=0):
    return datetime(y, mo, d, h, mi)


class TestBaseDelay:
    def test_base_delay_in_range(self):
        t = HumanizedTimer(seed=42)
        for _ in range(100):
            d = t.base_delay()
            assert 2.5 <= d <= 8.0

    def test_jitter_in_range(self):
        t = HumanizedTimer(seed=42)
        for _ in range(100):
            j = t.jitter()
            assert 0.1 <= j <= 1.5

    def test_fatigue_increases_with_orders(self):
        t = HumanizedTimer(seed=42)
        assert t.fatigue_drift() == 0.0
        t.record_action()
        d1 = t.fatigue_drift()
        t.record_action()
        d2 = t.fatigue_drift()
        assert d2 > d1

    def test_fatigue_max_cap(self):
        t = HumanizedTimer(seed=42)
        for _ in range(50):
            t.record_action()
        assert t.fatigue_drift() <= 5.0

    def test_reset_orders_today(self):
        t = HumanizedTimer(seed=42)
        for _ in range(10):
            t.record_action()
        t.reset_orders_today()
        assert t.fatigue_drift() == 0.0


class TestHesitation:
    def test_hesitation_occasionally_long(self):
        t = HumanizedTimer(seed=42)
        long_delays = []
        for _ in range(200):
            d = t.hesitation_delay()
            if d > 0:
                long_delays.append(d)
        # With 8% chance over 200 calls, we expect ~16 hesitations
        assert len(long_delays) > 0
        for d in long_delays:
            assert 5.0 <= d <= 20.0


class TestComputeDelay:
    def test_compute_delay_positive(self):
        t = HumanizedTimer(seed=42)
        now = _dt()
        for _ in range(50):
            delay = t.compute_delay(now)
            assert delay > 0

    def test_compute_delay_increases_with_fatigue(self):
        t = HumanizedTimer(seed=42)
        now = _dt()
        delays_before = [t.compute_delay(now) for _ in range(5)]
        for _ in range(10):
            t.record_action()
        delays_after = [t.compute_delay(now) for _ in range(5)]
        # Average delay should be higher after fatigue
        assert sum(delays_after) / len(delays_after) > sum(delays_before) / len(delays_before)


class TestNewsAwareDelay:
    def test_no_delay_without_news(self):
        t = HumanizedTimer(seed=42)
        d = t.news_aware_delay(_dt())
        assert d == 0.0

    def test_delay_near_high_impact_news(self):
        events = [{"time": _dt(h=10, mi=1), "impact": "high"}]
        t = HumanizedTimer(seed=42, news_calendar=events)
        d = t.news_aware_delay(_dt(h=10, mi=0))  # 1 min away
        assert 15.0 <= d <= 90.0

    def test_no_delay_far_from_news(self):
        events = [{"time": _dt(h=15, mi=0), "impact": "high"}]
        t = HumanizedTimer(seed=42, news_calendar=events)
        d = t.news_aware_delay(_dt(h=10, mi=0))  # 5 hours away
        assert d == 0.0

    def test_no_delay_for_medium_impact(self):
        events = [{"time": _dt(h=10, mi=1), "impact": "medium"}]
        t = HumanizedTimer(seed=42, news_calendar=events)
        d = t.news_aware_delay(_dt(h=10, mi=0))
        assert d == 0.0


class TestEarnings:
    def test_earnings_day_default_false(self):
        t = HumanizedTimer(seed=42)
        assert t.is_earnings_day("TSLA", date(2026, 8, 25)) is False

    def test_set_news_events(self):
        t = HumanizedTimer(seed=42)
        t.set_news_events([{"time": _dt(), "impact": "high"}])
        assert len(t._news_events) == 1


class TestMinGap:
    def test_min_gap_ok_when_no_previous(self):
        t = HumanizedTimer(seed=42)
        assert t.is_min_gap_ok(None, _dt()) is True

    def test_min_gap_not_ok_too_soon(self):
        t = HumanizedTimer(seed=42)
        last = _dt(h=10, mi=0)
        now = _dt(h=10, mi=2)  # 2 min < 3 min min gap
        assert t.is_min_gap_ok(last, now) is False

    def test_min_gap_ok_after_sufficient_time(self):
        t = HumanizedTimer(seed=42)
        last = _dt(h=10, mi=0)
        now = _dt(h=10, mi=5)  # 5 min > 3 min min gap
        assert t.is_min_gap_ok(last, now) is True


class TestCloseDelay:
    def test_close_delay_in_range(self):
        t = HumanizedTimer(seed=42)
        for _ in range(50):
            d = t.close_delay()
            assert 1.0 <= d <= 5.0
