"""Tests for EquityCurveHumanizer."""

import pytest

from execution.stealth.equity_curve_humanizer import EquityCurveHumanizer


def _make_position(entry=2000, current=2005, stop=1995, tp=2010, side="long", ticket=1):
    return {
        "ticket": ticket,
        "id": ticket,
        "entry_price": entry,
        "current_price": current,
        "stop_price": stop,
        "tp_price": tp,
        "side": side,
        "volume": 0.10,
    }


def test_partial_exit_prob():
    humanizer = EquityCurveHumanizer(seed=42)
    # At +1R, long: entry 2000, stop 1995 => risk 5, +1R = 2005
    pos = _make_position(entry=2000, current=2005, stop=1995, tp=2010, side="long", ticket=1)
    # Sample 1000 times with fresh humanizer each time to avoid partial_done tracking
    hits = 0
    for i in range(1000):
        h = EquityCurveHumanizer(seed=i)
        if h.should_partial_exit(pos):
            hits += 1
    # 25% => allow 15%-35%
    assert 150 <= hits <= 350


def test_partial_exit_pct_range():
    humanizer = EquityCurveHumanizer(seed=42)
    pcts = [humanizer.get_partial_exit_pct() for _ in range(200)]
    assert all(0.30 <= p <= 0.50 for p in pcts)
    assert max(pcts) - min(pcts) > 0.1


def test_early_close_prob():
    humanizer = EquityCurveHumanizer(seed=42)
    # At 0.6*TP: entry 2000, tp 2010 => total 10, 0.6*10=6, current 2006
    pos = _make_position(entry=2000, current=2006, stop=1995, tp=2010, side="long", ticket=1)
    hits = 0
    for i in range(1000):
        h = EquityCurveHumanizer(seed=i)
        if h.should_early_close(pos):
            hits += 1
    # 12% => allow 5%-20%
    assert 50 <= hits <= 200


def test_trailing_at_1_5r():
    humanizer = EquityCurveHumanizer(seed=42)
    # +1.5R: entry 2000, stop 1995 => risk 5, +1.5R = 2007.5
    pos_below = _make_position(entry=2000, current=2006, stop=1995, tp=2010, side="long", ticket=1)
    pos_above = _make_position(entry=2000, current=2008, stop=1995, tp=2010, side="long", ticket=1)
    assert humanizer.should_trail(pos_below) is False
    assert humanizer.should_trail(pos_above) is True


def test_trailing_distance_range():
    humanizer = EquityCurveHumanizer(seed=42, pip_value=0.1)
    distances = [humanizer.get_trailing_distance_price() for _ in range(200)]
    # 15-40 pips * 0.1 = 1.5-4.0 price units
    assert all(1.5 <= d <= 4.0 for d in distances)
    pips = [humanizer.get_trailing_distance_pips() for _ in range(200)]
    assert all(15 <= p <= 40 for p in pips)


def test_manage_position_actions():
    humanizer = EquityCurveHumanizer(seed=42)
    pos = _make_position(entry=2000, current=2008, stop=1995, tp=2010, side="long", ticket=1)
    actions = humanizer.manage_position(pos)
    # At +1.6R (2008), should trigger trailing, maybe partial and early close
    # At least trailing should be present
    types = [a["type"] for a in actions]
    assert "trailing" in types


def test_partial_done_tracking():
    humanizer = EquityCurveHumanizer(seed=1)
    pos = _make_position(entry=2000, current=2005, stop=1995, tp=2010, side="long", ticket=1)
    # Force should_partial_exit to True by mocking rng? Instead we test that after a True, second call False
    # We'll manually set prob to 1.0
    humanizer.PARTIAL_EXIT_PROB = 1.0
    assert humanizer.should_partial_exit(pos) is True
    # Second time same ticket should be False (already done)
    assert humanizer.should_partial_exit(pos) is False


def test_seed_reproducibility():
    h1 = EquityCurveHumanizer(seed=12345)
    h2 = EquityCurveHumanizer(seed=12345)
    pos = _make_position(entry=2000, current=2005, stop=1995, tp=2010, side="long", ticket=1)
    # Both should give same decisions for same seed sequence
    a1 = h1.should_partial_exit(pos)
    a2 = h2.should_partial_exit(pos)
    assert a1 == a2
