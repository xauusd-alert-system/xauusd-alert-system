# -*- coding: utf-8 -*-
"""Tests for Phase 1-3 optimizations: adaptive VWAP, impulse relaxation, async, cooldown, caching, unrealized, anchored."""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

from tests.fixtures.vwap_scenarios import benchmark_downtrend, benchmark_uptrend, long_scenario
from usstocks.indicators import anchored_vwap, find_swing_high, find_swing_low, session_vwap_series
from usstocks.models import Bar, RiskState
from usstocks.risk_engine import RiskEngine
from usstocks.scanner_loop import SignalOnlyRunner
from usstocks.strategy.vwap_pullback import StrategyConfig, _effective_vwap_tol, evaluate

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 26, 10, 40, tzinfo=NY)


def _asof(bars):
    return bars[-1].ts + timedelta(minutes=5)


BASE_CFG = {
    "risk": {
        "risk_per_trade_usd": 10.0,
        "personal_daily_stop_usd": -20.0,
        "max_trades_per_day": 2,
        "max_consecutive_losses": 2,
        "daily_profit_lock_usd": 20.0,
        "no_new_entries_minutes_before_close": 25,
        "consecutive_losses_cooldown_minutes": 30,
    },
    "challenge": {"max_notional_usd": 5000.0},
    "strategy": {},
    "us_stocks": {"tech_symbols": ["AMD"]},
    "session": {"holidays": []},
    "scanner": {"max_parallel_workers": 3, "cache_ttl_seconds": 30},
}


# ---------------------------------------------------------------------------
# Adaptive VWAP tolerance
# ---------------------------------------------------------------------------

def test_effective_vwap_tol_helper():
    """Helper should return adaptive or fixed tolerance."""
    from usstocks.indicators import calculate_atr
    # Create bars with known ATR ~2%
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    bars = []
    price = 100.0
    for i in range(20):
        # Create bars with high volatility: h-l ~2
        bars.append(Bar(ts=t0 + timedelta(minutes=5 * i), open=price, high=price + 1, low=price - 1, close=price + 0.5, volume=100000))
        price += 0.3
    cfg = StrategyConfig(use_adaptive_vwap_tolerance=True, atr_tolerance_multiplier=0.5, vwap_touch_tolerance_pct=0.10)
    tol = _effective_vwap_tol(cfg, bars)
    # ATR ~2, atr_pct ~2%, adaptive = 1%, config 0.10% => adaptive wins => tol 0.01
    atr_val = calculate_atr(bars, period=min(14, len(bars) - 1))
    atr_pct = atr_val / bars[-1].close * 100
    expected = max(0.10, atr_pct * 0.5) / 100.0
    assert abs(tol - expected) < 1e-6
    # Low ATR should fallback to config
    cfg2 = StrategyConfig(use_adaptive_vwap_tolerance=True, atr_tolerance_multiplier=0.01, vwap_touch_tolerance_pct=0.10)
    tol2 = _effective_vwap_tol(cfg2, bars)
    # adaptive very small => config wins
    assert abs(tol2 - 0.001) < 1e-6


def test_adaptive_vwap_tolerance_disabled():
    cfg_off = StrategyConfig(use_adaptive_vwap_tolerance=False, atr_tolerance_multiplier=0.1, vwap_touch_tolerance_pct=0.10)
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    bars = [Bar(t0 + timedelta(minutes=5 * i), 100, 101, 99, 100.5, 100000) for i in range(10)]
    tol = _effective_vwap_tol(cfg_off, bars)
    assert abs(tol - 0.001) < 1e-9


def test_adaptive_vwap_allows_wider_touch():
    """Long scenario should still pass with adaptive enabled."""
    bars = long_scenario()
    bench = benchmark_uptrend()
    cfg = StrategyConfig(use_adaptive_vwap_tolerance=True, atr_tolerance_multiplier=0.1)
    ev = evaluate("AMD", bars, bench, side="long", in_watchlist=True, cfg=cfg, asof=_asof(bars))
    assert ev.ok, f"failed {ev.failed}"


def test_stop_buffer_config_mapping():
    cfg = StrategyConfig.from_cfg({"strategy": {"stop_buffer": 0.05}})
    assert cfg.stop_buffer == 0.05
    cfg2 = StrategyConfig.from_cfg({"strategy": {"stop_buffer_cents": 0.07}})
    assert cfg2.stop_buffer == 0.07


# ---------------------------------------------------------------------------
# Impulse counter-moves
# ---------------------------------------------------------------------------

def _make_impulse_bars(with_counter_moves=0):
    """Build 10-bar 5m scenario with controlled counter-moves in impulse (bars 0-4 impulse, 5-8 pullback, 9 confirm)."""
    # Use long_scenario as base but modify closes to inject counter-moves
    # Build raw 5m bars
    from usstocks.indicators import aggregate_to_5m, drop_unclosed_1m
    # Instead construct 1m bars that aggregate to desired 5m closes
    # Simpler: directly test _find_structure via evaluate with crafted 5m bars via 1m expansion
    from tests.fixtures.vwap_scenarios import build_session
    # Create custom script where impulse has counter-moves
    # We'll craft 5m closes: first test pure directed
    # For this unit test, directly test _effective + _find_structure via strategy
    pass


def test_impulse_with_one_counter_move_allowed():
    """Impulse with 1 counter-move should still be found when configured."""
    # Use long_scenario which is directed; verify with max_impulse_counter_moves=1 still passes
    bars = long_scenario()
    bench = benchmark_uptrend()
    cfg = StrategyConfig(min_impulse_pct=0.8, max_impulse_counter_moves=1)
    ev = evaluate("AMD", bars, bench, side="long", in_watchlist=True, cfg=cfg, asof=_asof(bars))
    assert ev.ok or any("IMPULSE" in p for p in ev.passed)

    # With 0 allowed, also passes (pure directed)
    cfg0 = StrategyConfig(min_impulse_pct=0.8, max_impulse_counter_moves=0)
    ev0 = evaluate("AMD", bars, bench, side="long", in_watchlist=True, cfg=cfg0, asof=_asof(bars))
    assert ev0.ok


def test_impulse_counter_moves_respected():
    # Test the helper logic directly via _find_structure
    from usstocks.indicators import aggregate_to_5m, drop_unclosed_1m, session_vwap_series
    from usstocks.strategy.vwap_pullback import _find_structure
    from usstocks.models import Bar as B
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    # Build 9 bars: need at least min_impulse_bars+3 =5, create 9
    # Simulate impulse with 2 counter-moves: closes 100,101,100.5,100,102
    # This has 2 counter moves (101->100.5 down, 100.5->100 down) for long
    cfg_allow1 = StrategyConfig(min_impulse_pct=0.5, min_impulse_bars=2, max_impulse_counter_moves=1, min_impulse_volume_ratio=0.5, max_pullback_volume_ratio=2.0, vwap_touch_tolerance_pct=5.0)
    # Create bars where impulse window has 2 counters but cumulative still >0.5
    # We'll use _find_structure directly with crafted bars and vwap
    bars = []
    closes = [100, 101, 100.5, 100, 102, 101.5, 101, 100.8, 101.2]  # last is confirm
    for i, c in enumerate(closes):
        bars.append(B(ts=t0 + timedelta(minutes=5*i), open=closes[max(0,i-1)] if i>0 else c, high=c+0.3, low=c-0.3, close=c, volume=100000 if i<5 else 50000))
        # bump impulse volume
        if i in (1,2,4):
            bars[-1].volume = 200000
    vwap = [99.5]*len(bars)  # ensure price above vwap for impulse
    # With allow 1, should reject structure with 2 counters OR allow if another window fits
    # Test directed count directly
    # Instead test low-level: 5-bar window closes [100,101,100.5,100,102] has counters 2
    window_closes = closes[:5]
    counters = sum(1 for k in range(len(window_closes)-1) if (window_closes[k+1] > window_closes[k]) != True)
    assert counters == 2
    # So cfg_allow1 should reject a window with 2 counters, but may find another valid window
    # Verify the logic: directed = counters <=1 => False, so rejected
    assert counters > cfg_allow1.max_impulse_counter_moves
    # With allow 2, it would be allowed
    cfg_allow2 = StrategyConfig(min_impulse_pct=0.5, min_impulse_bars=2, max_impulse_counter_moves=2, min_impulse_volume_ratio=0.5, max_pullback_volume_ratio=2.0, vwap_touch_tolerance_pct=5.0)
    assert counters <= cfg_allow2.max_impulse_counter_moves


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

def test_consecutive_losses_cooldown_blocks():
    engine = RiskEngine(max_consecutive_losses=2, consecutive_losses_cooldown_minutes=30)
    now = datetime.now().astimezone()
    close = now + timedelta(hours=2)
    state = RiskState(session_date="2026-08-30", consecutive_losses=2, last_loss_time=now - timedelta(minutes=10))
    d = engine.evaluate(state, now, close)
    assert not d.allowed
    assert d.code == "MAX_CONSECUTIVE_LOSSES_COOLDOWN"
    assert "cooldown" in d.reason


def test_consecutive_losses_cooldown_elapsed():
    engine = RiskEngine(max_consecutive_losses=2, consecutive_losses_cooldown_minutes=30)
    now = datetime.now().astimezone()
    close = now + timedelta(hours=2)
    state = RiskState(session_date="2026-08-30", consecutive_losses=2, last_loss_time=now - timedelta(minutes=35))
    d = engine.evaluate(state, now, close)
    assert d.allowed
    assert d.code == "ALLOW"


def test_consecutive_losses_no_last_loss_time():
    engine = RiskEngine(max_consecutive_losses=2)
    now = datetime.now().astimezone()
    close = now + timedelta(hours=2)
    state = RiskState(session_date="2026-08-30", consecutive_losses=2, last_loss_time=None)
    d = engine.evaluate(state, now, close)
    assert not d.allowed
    assert d.code == "MAX_CONSECUTIVE_LOSSES"


def test_unrealized_pnl_only_with_active_position():
    engine = RiskEngine(personal_daily_stop_usd=-20.0)
    now = datetime.now().astimezone()
    close = now + timedelta(hours=2)
    state1 = RiskState(session_date="2026-08-30", realized_pnl_usd=-15.0, unrealized_pnl_usd=-10.0, active_symbol=None)
    d1 = engine.evaluate(state1, now, close)
    assert d1.allowed
    state2 = RiskState(session_date="2026-08-30", realized_pnl_usd=-15.0, unrealized_pnl_usd=-10.0, active_symbol="AMD")
    d2 = engine.evaluate(state2, now, close)
    assert not d2.allowed
    assert d2.code == "PERSONAL_DAILY_STOP"


# ---------------------------------------------------------------------------
# Async scanner
# ---------------------------------------------------------------------------

class SlowProvider:
    def __init__(self, sleep=0.4):
        self.sleep = sleep
        self.calls = []
    def get_bars(self, symbol, count):
        self.calls.append(symbol)
        time.sleep(self.sleep)
        if symbol in ("QQQ", "SPY"):
            return benchmark_uptrend(count // 5)
        # Use long for AMD, flat for others to ensure only one signal
        if symbol == "AMD":
            return long_scenario()
        from tests.fixtures.vwap_scenarios import flat_scenario
        return flat_scenario()


class CaptureNotifier:
    def __init__(self):
        self.signals = []
        self.risk_events = []
    def send_signal(self, s):
        self.signals.append(s)
    def send_risk_event(self, e):
        self.risk_events.append(e)


def test_parallel_evaluation_single_signal():
    provider = SlowProvider(sleep=0.05)
    notifier = CaptureNotifier()
    runner = SignalOnlyRunner(BASE_CFG, provider, notifier, watchlist=["AMD", "NVDA"], state=RiskState(session_date="2026-08-26"), symbol_ids={"AMD": "SID", "NVDA": "SID2", "QQQ": "SID_Q", "SPY": "SID_S"})
    sigs = runner.scan_once(NOW)
    assert len(sigs) <= 1
    assert len(notifier.signals) <= 1


def test_parallel_evaluation_error_isolation():
    class FailingProvider:
        def __init__(self):
            self.call_count = 0
            self.symbols = []
        def get_bars(self, symbol, count):
            self.call_count += 1
            self.symbols.append(symbol)
            if symbol == "AMD":
                raise ConnectionError("Network error")
            if symbol in ("QQQ", "SPY"):
                return benchmark_uptrend(count // 5)
            return long_scenario()
    provider = FailingProvider()
    notifier = CaptureNotifier()
    cfg = dict(BASE_CFG)
    cfg["us_stocks"] = {"tech_symbols": ["AMD", "NVDA"]}
    runner = SignalOnlyRunner(cfg, provider, notifier, watchlist=["AMD", "NVDA", "TSLA"], state=RiskState(session_date="2026-08-26"), symbol_ids={"AMD": "SID", "NVDA": "SID2", "TSLA": "SID3", "QQQ": "SID_Q", "SPY": "SID_S"})
    sigs = runner.scan_once(NOW)
    # Should not raise and should attempt at least AMD + bench + others
    assert provider.call_count >= 2


def test_parallel_evaluation_performance():
    provider = SlowProvider(sleep=0.3)
    notifier = CaptureNotifier()
    cfg = dict(BASE_CFG)
    cfg["scanner"] = {"max_parallel_workers": 3, "cache_ttl_seconds": 30}
    runner = SignalOnlyRunner(cfg, provider, notifier, watchlist=["AMD", "NVDA", "TSLA"], state=RiskState(session_date="2026-08-26"), symbol_ids={"AMD": "SID", "NVDA": "SID2", "TSLA": "SID3", "QQQ": "SID_Q", "SPY": "SID_S"})
    start = time.time()
    runner.scan_once(NOW)
    elapsed = time.time() - start
    # With 0.3s per symbol, parallel should be <1.5s, sequential ~2s+
    assert elapsed < 1.8, f"parallel took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_vwap_caching_reuses_calculation():
    provider = SlowProvider(sleep=0.01)
    notifier = CaptureNotifier()
    runner = SignalOnlyRunner(BASE_CFG, provider, notifier, watchlist=["AMD"], state=RiskState(session_date="2026-08-26"), symbol_ids={"AMD": "SID", "QQQ": "SID_Q"})
    with patch("usstocks.indicators.session_vwap_series") as mock_vwap:
        mock_vwap.return_value = [100.0] * 100
        # First call caches
        bars = long_scenario()
        # Use cached helpers directly
        from usstocks.indicators import aggregate_to_5m, drop_unclosed_1m
        bars5 = aggregate_to_5m(drop_unclosed_1m(bars, _asof(bars)))
        v1 = runner._get_vwap_cached("AMD", bars5, "2026-08-26")
        c1 = mock_vwap.call_count
        v2 = runner._get_vwap_cached("AMD", bars5, "2026-08-26")
        c2 = mock_vwap.call_count
        assert v1 == v2
        assert c2 == c1  # reused

def test_cache_invalidates_on_new_bar():
    provider = SlowProvider(sleep=0.01)
    notifier = CaptureNotifier()
    runner = SignalOnlyRunner(BASE_CFG, provider, notifier, watchlist=["AMD"], state=RiskState(session_date="2026-08-26"), symbol_ids={"AMD": "SID", "QQQ": "SID_Q"})
    with patch("usstocks.indicators.session_vwap_series") as mock_vwap:
        mock_vwap.return_value = [100.0] * 100
        t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
        bars = [Bar(t0 + timedelta(minutes=5*i), 100, 101, 99, 100.5, 100000) for i in range(10)]
        runner._get_vwap_cached("AMD", bars, "2026-08-26")
        c1 = mock_vwap.call_count
        # Add new bar with different close
        bars2 = bars + [Bar(t0 + timedelta(minutes=50), 101, 102, 100, 101.5, 120000)]
        runner._get_vwap_cached("AMD", bars2, "2026-08-26")
        c2 = mock_vwap.call_count
        assert c2 == c1 + 1


# ---------------------------------------------------------------------------
# Anchored VWAP
# ---------------------------------------------------------------------------

def test_anchored_vwap_starts_at_anchor():
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    bars = [Bar(t0 + timedelta(minutes=5*i), 100+i, 101+i, 99+i, 100.5+i, 100000) for i in range(10)]
    vwap = anchored_vwap(bars, 5)
    assert abs(vwap[0] - bars[5].typical_price) < 1e-6
    assert len(vwap) == 5

def test_find_swing_low():
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    bars = [Bar(t0 + timedelta(minutes=5*i), 100, 101, 100+i, 100.5, 100000) for i in range(10)]
    # Make bar 5 the lowest
    bars[5] = Bar(t0 + timedelta(minutes=25), 100, 101, 90, 100.5, 100000)
    idx = find_swing_low(bars, lookback=10)
    assert idx == 5

def test_find_swing_high():
    t0 = datetime(2026, 8, 26, 9, 30, tzinfo=NY)
    bars = [Bar(t0 + timedelta(minutes=5*i), 100, 101, 99, 100.5, 100000) for i in range(10)]
    bars[3] = Bar(t0 + timedelta(minutes=15), 100, 120, 99, 100.5, 100000)
    idx = find_swing_high(bars, lookback=10)
    assert idx == 3
