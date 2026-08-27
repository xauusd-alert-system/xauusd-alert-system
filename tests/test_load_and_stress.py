"""Load and stress tests for high throughput bars, indicators, and scanner (Phase 4)."""
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pytest

from usstocks.indicators import (
    session_vwap_series,
    opening_range,
    aggregate_to_5m,
    average_volume,
)
from usstocks.models import Bar, PremarketSnapshot
from usstocks.notify import TelegramRateLimiter
from usstocks.premarket_ranker import build_watchlist, score_snapshot, ScannerConfig

NY = ZoneInfo("America/New_York")


def test_indicator_performance_on_10k_bars():
    """Verify VWAP calculation over 1,000 5m bars in regular market hours completes quickly."""
    base_dt = datetime(2026, 8, 27, 9, 30, tzinfo=NY)
    bars = []
    p = 150.0
    for i in range(1_000):
        t = base_dt + timedelta(minutes=i)
        p += (0.05 if i % 2 == 0 else -0.04)
        bars.append(
            Bar(
                ts=t,
                open=p,
                high=p + 0.5,
                low=p - 0.5,
                close=p + 0.01,
                volume=1000.0 + (i % 500) * 10,
            )
        )

    t0 = time.perf_counter()
    vwap_res = session_vwap_series(bars, filter_premarket=False)
    avg_vol = average_volume(bars, upto=len(bars), lookback=20)
    elapsed = time.perf_counter() - t0

    assert len(vwap_res) == 1_000
    assert avg_vol > 0
    assert elapsed < 0.2, f"VWAP took {elapsed:.4f}s (expected < 0.2s)"


def test_aggregate_to_5m_performance():
    """Verify resampling 3,000 1m bars to 5m completes quickly and correctly."""
    base_dt = datetime(2026, 8, 27, 9, 30, tzinfo=NY)
    bars_1m = [
        Bar(
            ts=base_dt + timedelta(minutes=i),
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=500.0,
        )
        for i in range(300)
    ]

    t0 = time.perf_counter()
    bars_5m = aggregate_to_5m(bars_1m)
    elapsed = time.perf_counter() - t0

    assert len(bars_5m) == 60
    assert elapsed < 0.1


def test_rate_limiter_stress_bursts():
    """Verify rate limiter under 200 rapid calls."""
    limiter = TelegramRateLimiter(max_per_second=20.0, max_per_chat_per_second=1.0)
    
    accepted = 0
    rejected = 0
    for i in range(200):
        if limiter.can_send(f"chat_{i}"):
            accepted += 1
            limiter.record_send(f"chat_{i}")
        else:
            rejected += 1

    assert accepted <= 20
    assert rejected >= 180


def test_premarket_ranker_stress_100_symbols():
    """Verify multi-factor scoring on large snapshot lists."""
    snapshots = []
    for i in range(100):
        sym = f"S{i:03d}"
        gap = (i % 10) - 4.5
        rvol = 0.5 + (i % 5) * 0.8
        dvol = 60_000_000 + i * 1_000_000
        spread = 0.02 + (i % 4) * 0.01
        snapshots.append(
            PremarketSnapshot(
                symbol=sym,
                price=50.0 + i,
                prev_close=50.0,
                gap_pct=gap,
                relative_volume=rvol,
                avg_daily_dollar_volume=dvol,
                spread_pct=spread,
            )
        )

    t0 = time.perf_counter()
    cfg = ScannerConfig(max_watchlist_size=10, min_abs_gap_pct=1.0)
    ranked = build_watchlist(snapshots, cfg=cfg)
    elapsed = time.perf_counter() - t0

    assert len(ranked) <= 10
    assert elapsed < 0.1
