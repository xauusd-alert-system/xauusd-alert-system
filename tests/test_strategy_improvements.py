"""Tests for Phase 5 Strategy Improvements: Volatility filter, Time stop, Volume spike/climax, News filter."""
from datetime import datetime, timedelta
import pytest
from zoneinfo import ZoneInfo

from tests.fixtures.vwap_scenarios import benchmark_uptrend, long_scenario
from usstocks.models import Bar
from usstocks.strategy.vwap_pullback import StrategyConfig, evaluate

NY = ZoneInfo("America/New_York")


def _asof(bars):
    return bars[-1].ts + timedelta(minutes=5)


def test_volatility_filter_blocks_low_atr():
    bars = long_scenario()
    bench = benchmark_uptrend()
    
    # Require very high ATR (e.g. 50%) -> should block
    cfg_high_atr = StrategyConfig(min_atr_pct=50.0)
    ev = evaluate("AMD", bars, bench, side="long", in_watchlist=True, cfg=cfg_high_atr, asof=_asof(bars))
    assert not ev.ok
    assert any(f.startswith("VOLATILITY_FILTER") for f in ev.failed)

    # Normal ATR -> passes
    cfg_normal = StrategyConfig(min_atr_pct=0.1)
    ev_ok = evaluate("AMD", bars, bench, side="long", in_watchlist=True, cfg=cfg_normal, asof=_asof(bars))
    assert ev_ok.ok
    assert any(p.startswith("VOLATILITY_FILTER") for p in ev_ok.passed)


def test_time_stop_bars_filter():
    bars = long_scenario()
    bench = benchmark_uptrend()
    
    # Restrict max pullback to 1 bar -> should fail structure
    cfg_time_stop = StrategyConfig(time_stop_bars=1)
    ev = evaluate("AMD", bars, bench, side="long", in_watchlist=True, cfg=cfg_time_stop, asof=_asof(bars))
    assert not ev.ok
    assert any(f.startswith("STRUCTURE_IMPULSE_PULLBACK") for f in ev.failed)


def test_volume_spike_climax_filter():
    bars = long_scenario()
    bench = benchmark_uptrend()
    
    # Set max climax volume ratio very low -> should reject structure
    cfg_climax = StrategyConfig(max_climax_volume_ratio=0.1)
    ev = evaluate("AMD", bars, bench, side="long", in_watchlist=True, cfg=cfg_climax, asof=_asof(bars))
    assert not ev.ok
    assert any(f.startswith("STRUCTURE_IMPULSE_PULLBACK") for f in ev.failed)


def test_news_filter_blocks_signal():
    bars = long_scenario()
    bench = benchmark_uptrend()
    cfg = StrategyConfig()
    
    # When news_blocked=True
    ev = evaluate("AMD", bars, bench, side="long", in_watchlist=True, cfg=cfg, asof=_asof(bars), news_blocked=True)
    assert not ev.ok
    assert any(f.startswith("NEWS_FILTER") for f in ev.failed)
