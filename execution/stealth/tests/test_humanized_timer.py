"""Tests for HumanizedTimer."""

from datetime import datetime, timezone, timedelta

import pytest

from execution.stealth.humanized_timer import HumanizedTimer


def test_entry_delay_in_range():
    timer = HumanizedTimer(seed=123)
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    delays = [timer.get_entry_delay(now) for _ in range(200)]
    # Base 2.5-8 + jitter 0.1-1.5 = min ~2.6 max ~9.5 without fatigue/news/hesitation
    # With hesitation +5-20 max ~29.5, with news +15-90 max ~119.5
    # So check at least base range present
    assert all(d >= 2.5 for d in delays)
    # Without news, most should be < 30 sec (hesitation is 8%)
    # Allow some over due to hesitation
    assert sum(1 for d in delays if d > 30) < 30  # less than 15% over 30

    # Check distribution: should have variance
    assert max(delays) - min(delays) > 2.0


def test_news_aware_delay():
    # News at 10:00, signal at 10:01 => within 2min window, should add 15-90s
    news_time = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    timer_no_news = HumanizedTimer(seed=42, news_calendar=[])
    timer_with_news = HumanizedTimer(
        seed=42, news_calendar=[{"time": news_time, "impact": "high"}]
    )
    now = datetime(2026, 8, 20, 10, 1, tzinfo=timezone.utc)
    # Use same seed, so base should be similar but news adds extra
    # We'll sample many and compare averages
    no_news_delays = [timer_no_news.get_entry_delay(now) for _ in range(100)]
    with_news_delays = [timer_with_news.get_entry_delay(now) for _ in range(100)]
    avg_no = sum(no_news_delays) / len(no_news_delays)
    avg_with = sum(with_news_delays) / len(with_news_delays)
    # With news should be larger on average by at least 10 sec (15-90 range)
    assert avg_with > avg_no + 5


def test_news_outside_window_no_extra():
    news_time = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    timer = HumanizedTimer(
        seed=42, news_calendar=[{"time": news_time, "impact": "high"}]
    )
    # 10 minutes later, outside 2min window
    now = datetime(2026, 8, 20, 10, 10, tzinfo=timezone.utc)
    delays = [timer.get_entry_delay(now) for _ in range(50)]
    # Should not have news extra, so max < 30 sec mostly
    assert sum(1 for d in delays if d > 35) < 10


def test_fatigue_drift():
    timer = HumanizedTimer(seed=1)
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    # First delay without orders
    d0 = timer.get_entry_delay(now)
    # Simulate 10 orders today
    for i in range(10):
        timer.record_order(now + timedelta(minutes=i))
    d10 = timer.get_entry_delay(now + timedelta(minutes=11))
    # Fatigue should increase delay: 10 * 0.3 = 3 sec max 5
    # So average should be higher, but due to randomness we check drift tracking
    assert timer._fatigue_drift == min(10 * 0.3, 5.0)
    # d10 should on average be larger than d0 - but allow randomness
    # Sample many after fatigue
    timer2 = HumanizedTimer(seed=1)
    timer2._current_day = now.date()
    timer2._orders_today = 10
    timer2._fatigue_drift = min(10 * 0.3, 5.0)
    delays_fatigued = [timer2.get_entry_delay(now) for _ in range(100)]
    timer_fresh = HumanizedTimer(seed=1)
    timer_fresh._current_day = now.date()
    timer_fresh._orders_today = 0
    delays_fresh = [timer_fresh.get_entry_delay(now) for _ in range(100)]
    assert sum(delays_fatigued) / 100 > sum(delays_fresh) / 100


def test_hesitation_prob():
    timer = HumanizedTimer(seed=999)
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    delays = [timer.get_entry_delay(now) for _ in range(1000)]
    # Hesitation 8% chance +5-20s, so count of delays > 15 sec should be around 8% + some base >?
    # Base max 9.5, so >15 indicates hesitation or news. Without news, >15 should be ~8%
    count_hes = sum(1 for d in delays if d >= 14.5)  # base max 9.5, so >=14.5 likely hesitation
    # 8% of 1000 = 80, allow 40-150
    assert 30 <= count_hes <= 150


def test_min_gap():
    timer = HumanizedTimer(seed=42)
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    assert timer.is_min_gap_ok(now) is True  # no last order
    timer.record_order(now)
    # Immediately after, gap not ok (gap 3-15 min)
    assert timer.is_min_gap_ok(now + timedelta(seconds=10)) is False
    # After max gap (15 min) should be ok
    assert timer.is_min_gap_ok(now + timedelta(minutes=16)) is True
    # Check gap range
    gap = timer.get_current_min_gap()
    assert 180 <= gap <= 900


def test_close_delay_range():
    timer = HumanizedTimer(seed=123)
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    delays = [timer.get_close_delay(now) for _ in range(100)]
    assert all(1.0 <= d <= 7.0 for d in delays)  # 1-5 + up to 2 fatigue


def test_seed_reproducibility():
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    t1 = HumanizedTimer(seed=12345)
    t2 = HumanizedTimer(seed=12345)
    d1 = [t1.get_entry_delay(now) for _ in range(10)]
    d2 = [t2.get_entry_delay(now) for _ in range(10)]
    assert d1 == d2
