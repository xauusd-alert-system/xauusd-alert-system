# -*- coding: utf-8 -*-
"""ТЗ §12.4-7: valid long, invalid long (close below VWAP), valid short,
benchmark conflict blocks the signal."""
from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

from usstocks.models import Bar
from usstocks.strategy.vwap_pullback import Evaluation, StrategyConfig, evaluate
from tests.fixtures.vwap_scenarios import (
    NY,
    benchmark_downtrend,
    benchmark_uptrend,
    flat_scenario,
    long_scenario,
    short_scenario,
)

CFG = StrategyConfig()
ASOF = None  # set per test: last bar close moment


def _asof(bars):
    # everything fully closed: last bar's bucket ends 5 minutes after its open
    return bars[-1].ts + timedelta(minutes=5)


def _ev_long(sym="AMD", bars=None, bench=None, **kw):
    bars = bars if bars is not None else long_scenario()
    bench = bench if bench is not None else benchmark_uptrend()
    return evaluate(sym, bars, bench,
                    side="long", in_watchlist=True, cfg=CFG,
                    asof=_asof(bars), **kw)


def test_valid_long_produces_signal_with_full_plan():
    ev = _ev_long()
    assert ev.ok, f"failed checks: {ev.failed}"
    s = ev.signal
    assert s.side == "long"
    assert s.entry_high > s.stop > 0
    assert abs((s.tp1 - s.entry_high) / (s.entry_high - s.stop) - 1.0) < 1e-6
    assert abs((s.tp2 - s.entry_high) / (s.entry_high - s.stop) - 2.0) < 1e-6
    assert s.shares > 0 and s.notional_usd <= 5000 and s.planned_risk_usd <= 10
    codes = {p.split(":")[0] for p in ev.passed}
    for expected in ("VWAP_TOUCH", "CONFIRM_CLOSE_VWAP", "STRUCTURE_HL_LH",
                     "BENCHMARK_VWAP", "OPENING_RANGE_FILTER", "SIZING"):
        assert expected in codes


def test_invalid_long_confirmation_below_vwap_rejected():
    bars = long_scenario()
    fixed = list(bars[:-5])
    for b in bars[-5:]:                     # rewrite the confirmation bucket
        from usstocks.models import Bar
        fixed.append(Bar(ts=b.ts, open=b.open, high=max(b.high, b.close),
                         low=min(b.low, b.close), close=b.close - 0.35,
                         volume=b.volume))
    ev = _ev_long(bars=fixed)
    assert not ev.ok
    assert any(f.startswith("CONFIRM_CLOSE_VWAP") for f in ev.failed)


def test_valid_short_mirrored():
    from usstocks.strategy.vwap_pullback import evaluate as _e
    bars = short_scenario()
    ev = evaluate("NVDA", bars, benchmark_downtrend(), side="short",
                  in_watchlist=True, cfg=CFG, asof=_asof(bars))
    assert ev.ok, f"failed checks: {ev.failed}"
    s = ev.signal
    assert s.side == "short" and s.stop > s.entry_high
    assert s.tp2 < s.tp1 < s.entry_high
    assert s.shares > 0 and s.planned_risk_usd <= 10


def test_benchmark_conflict_blocks_long():
    ev = _ev_long(bench=benchmark_downtrend())
    assert not ev.ok
    assert any(f.startswith("BENCHMARK_VWAP") for f in ev.failed)


def test_missing_benchmark_fails_closed():
    ev = _ev_long(bench=[])
    assert not ev.ok
    assert any(f.startswith("BENCHMARK_VWAP") for f in ev.failed)


def test_not_in_watchlist_rejected():
    bars = long_scenario()
    ev = evaluate("AMD", bars, benchmark_uptrend(), side="long",
                  in_watchlist=False, cfg=CFG, asof=_asof(bars))
    assert not ev.ok
    assert any(f.startswith("WATCHLIST_MEMBER") for f in ev.failed)


def test_flat_session_no_structure_no_crash():
    bars = flat_scenario()
    ev = _ev_long(bars=bars)
    assert not ev.ok
    assert any(f.startswith("STRUCTURE_IMPULSE_PULLBACK") for f in ev.failed)


def test_room_to_level_block_when_resistance_too_close():
    ev = _ev_long(extra_levels=[_ev_long().signal.entry_high + 0.10])
    # nearest level only $0.10 above entry with risk ~$0.8 -> needs 1.8R room
    assert not ev.ok
    assert any(f.startswith("ROOM_TO_LEVEL") for f in ev.failed)


def test_evaluation_is_pure_and_repeatable():
    a, b = _ev_long(), _ev_long()
    assert a.passed == b.passed and a.failed == b.failed
