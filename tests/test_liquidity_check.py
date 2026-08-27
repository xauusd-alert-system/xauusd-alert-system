"""Tests for Liquidity and Spread filtering (P1-6)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from usstocks.models import PremarketSnapshot
from usstocks.premarket_ranker import ScannerConfig, passes_filters
from usstocks.strategy.vwap_pullback import StrategyConfig, evaluate
from tests.fixtures.vwap_scenarios import benchmark_uptrend, long_scenario

NY = ZoneInfo("America/New_York")


def test_scanner_spread_filter_blocks_wide_spread():
    cfg = ScannerConfig(max_spread_pct=0.10)
    snap_good = PremarketSnapshot(
        symbol="AAPL",
        price=150.0,
        prev_close=148.0,
        gap_pct=1.5,
        relative_volume=2.0,
        avg_daily_dollar_volume=100_000_000,
        spread_pct=0.05,
    )
    ok, _ = passes_filters(snap_good, cfg)
    assert ok

    snap_wide = PremarketSnapshot(
        symbol="ILLIQ",
        price=150.0,
        prev_close=148.0,
        gap_pct=1.5,
        relative_volume=2.0,
        avg_daily_dollar_volume=100_000_000,
        spread_pct=0.25,
    )
    ok_wide, reason = passes_filters(snap_wide, cfg)
    assert not ok_wide
    assert "spread" in reason


def test_strategy_liquidity_spread_filter_blocks_wide_spread():
    bars = long_scenario()
    bench = benchmark_uptrend()
    asof = bars[-1].ts + timedelta(minutes=5)
    cfg = StrategyConfig(max_spread_pct=0.10)

    # Clean spread passes
    ev_ok = evaluate(
        "AAPL", bars, bench, side="long", in_watchlist=True, cfg=cfg, asof=asof, spread_pct=0.04
    )
    assert ev_ok.ok
    assert any("LIQUIDITY_SPREAD" in p for p in ev_ok.passed)

    # Wide spread fails
    ev_wide = evaluate(
        "AAPL", bars, bench, side="long", in_watchlist=True, cfg=cfg, asof=asof, spread_pct=0.25
    )
    assert not ev_wide.ok
    assert any("LIQUIDITY_SPREAD" in f for f in ev_wide.failed)
