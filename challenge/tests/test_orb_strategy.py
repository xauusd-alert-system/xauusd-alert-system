"""Tests for ORBStrategy — UTEx challenge ORB."""

from datetime import datetime, timezone, date

import pytest

from challenge.orb_strategy import ORBStrategy


def test_orb_range_collection():
    strat = ORBStrategy(seed=42)
    now = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)
    # 3x 5-min candles within 0.3%-1.5% range filter (0.3-1.5 of ~100)
    candles = [
        {"open": 100, "high": 100.6, "low": 99.8, "close": 100.3, "volume": 2000000, "prev_close": 99.5},
        {"open": 100.3, "high": 100.7, "low": 99.9, "close": 100.5, "volume": 2100000, "prev_close": 99.5},
        {"open": 100.5, "high": 100.8, "low": 100.0, "close": 100.6, "volume": 2200000, "prev_close": 99.5},
    ]
    for i, c in enumerate(candles):
        t = now + timedelta(minutes=i*5)
        strat.update_5min_candle("AAPL", c, t)

    levels = strat.get_orb_levels("AAPL")
    assert levels is not None
    high, low = levels
    assert high == 100.8
    assert low == 99.8


def test_orb_filter_range_pct():
    strat = ORBStrategy(seed=42)
    now = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)

    # Range too small <0.3%
    small_range = [
        {"open": 100, "high": 100.1, "low": 100, "close": 100.05, "volume": 2000000, "prev_close": 99.9},
        {"open": 100.05, "high": 100.1, "low": 100, "close": 100.05, "volume": 2000000, "prev_close": 99.9},
        {"open": 100.05, "high": 100.1, "low": 100, "close": 100.05, "volume": 2000000, "prev_close": 99.9},
    ]
    for i, c in enumerate(small_range):
        t = now + timedelta(minutes=i*5)
        strat.update_5min_candle("AAPL", c, t)
    assert strat.get_orb_levels("AAPL") is None  # filtered

    # Range too large >1.5%
    strat2 = ORBStrategy(seed=42)
    large_range = [
        {"open": 100, "high": 105, "low": 95, "close": 100, "volume": 2000000, "prev_close": 99},
        {"open": 100, "high": 105, "low": 95, "close": 100, "volume": 2000000, "prev_close": 99},
        {"open": 100, "high": 105, "low": 95, "close": 100, "volume": 2000000, "prev_close": 99},
    ]
    for i, c in enumerate(large_range):
        t = now + timedelta(minutes=i*5)
        strat2.update_5min_candle("TSLA", c, t)
    assert strat2.get_orb_levels("TSLA") is None


def test_orb_filter_gap():
    strat = ORBStrategy(seed=42)
    now = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)

    # Gap >3% should skip
    gap_big = [
        {"open": 110, "high": 111, "low": 109.5, "close": 110.5, "volume": 2000000, "prev_close": 100},  # 10% gap
        {"open": 110.5, "high": 111, "low": 109.8, "close": 110.8, "volume": 2000000, "prev_close": 100},
        {"open": 110.8, "high": 111.2, "low": 110, "close": 111, "volume": 2000000, "prev_close": 100},
    ]
    for i, c in enumerate(gap_big):
        t = now + timedelta(minutes=i*5)
        strat.update_5min_candle("NVDA", c, t)
    assert strat.get_orb_levels("NVDA") is None


def test_orb_filter_earnings():
    strat = ORBStrategy(seed=42, earnings_calendar=[{"ticker": "TSLA", "date": date(2026, 8, 20)}])
    now = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)
    candles = [
        {"open": 100, "high": 101, "low": 99.5, "close": 100.5, "volume": 2000000, "prev_close": 99.5},
        {"open": 100.5, "high": 101.2, "low": 99.8, "close": 100.8, "volume": 2000000, "prev_close": 99.5},
        {"open": 100.8, "high": 101.5, "low": 100, "close": 101, "volume": 2000000, "prev_close": 99.5},
    ]
    for i, c in enumerate(candles):
        t = now + timedelta(minutes=i*5)
        strat.update_5min_candle("TSLA", c, t)
    assert strat.get_orb_levels("TSLA") is None


def test_orb_breakout_gap_direction():
    strat = ORBStrategy(seed=42)
    now_range = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)
    # Range with gap up (open > prev_close) within 0.3-1.5% filter
    candles = [
        {"open": 101, "high": 101.6, "low": 100.6, "close": 101.2, "volume": 2000000, "prev_close": 100},  # gap up 1%
        {"open": 101.2, "high": 101.7, "low": 100.7, "close": 101.4, "volume": 2000000, "prev_close": 100},
        {"open": 101.4, "high": 101.8, "low": 100.9, "close": 101.6, "volume": 2000000, "prev_close": 100},
    ]
    for i, c in enumerate(candles):
        t = now_range + timedelta(minutes=i*5)
        strat.update_5min_candle("AAPL", c, t)

    # Entry window 9:45-10:30, gap direction long, breakout long should pass, short should be blocked
    now_entry = datetime(2026, 8, 20, 9, 50, tzinfo=timezone.utc)
    # First breakout candle beyond high (101.8)
    breakout_candle = {"open": 101.9, "high": 102.2, "low": 101.8, "close": 102.0, "volume": 1000}
    sig = strat.check_breakout("AAPL", breakout_candle, now_entry)
    # First breakout stores pending, returns None
    assert sig is None

    # Second candle still beyond high -> confirmed
    now_entry2 = datetime(2026, 8, 20, 9, 51, tzinfo=timezone.utc)
    breakout_candle2 = {"open": 102.0, "high": 102.4, "low": 101.9, "close": 102.3, "volume": 1000}
    sig2 = strat.check_breakout("AAPL", breakout_candle2, now_entry2)
    assert sig2 is not None
    assert sig2.bias == "long"

    # Short breakout should be blocked when gap is long
    strat3 = ORBStrategy(seed=42)
    for i, c in enumerate(candles):
        t = now_range + timedelta(minutes=i*5)
        strat3.update_5min_candle("AAPL", c, t)
    short_candle = {"open": 100.5, "high": 100.8, "low": 99.5, "close": 99.8, "volume": 1000}
    sig_short = strat3.check_breakout("AAPL", short_candle, now_entry)
    # Even if breakout short, gap direction long should block
    assert sig_short is None


def test_orb_premarket_rotation():
    strat = ORBStrategy(seed=42)
    premarket = {"TSLA": 5000000, "AAPL": 3000000, "NVDA": 8000000, "AMZN": 1000000, "META": 2000000}
    selected = strat.select_tickers_by_premarket_volume(premarket, top_n=3)
    assert len(selected) == 3
    # Top 3 by volume: NVDA, TSLA, AAPL
    assert "NVDA" in selected
    assert "TSLA" in selected
    assert "AAPL" in selected


# Need timedelta import
from datetime import timedelta
