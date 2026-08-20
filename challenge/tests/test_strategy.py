"""Tests for the opening-range breakout strategy."""
from datetime import datetime

from challenge.strategy import OpeningRangeBreakout

CFG = {
    "session": {"start_local": "18:30", "end_local": "00:55"},
    "strategy": {"range_minutes": 30},
    "risk": {"stop_pct": 0.005, "tp_ratio": 1.5},
}


def _t(h, m):
    return datetime(2026, 8, 24, h, m)


def test_accumulates_range_then_long_breakout():
    s = OpeningRangeBreakout(CFG)
    assert s.update({"NVDA": {"last": 100.0}}, _t(18, 35)) == []
    assert s.update({"NVDA": {"last": 105.0}}, _t(18, 40)) == []
    sigs = s.update({"NVDA": {"last": 106.0}}, _t(19, 0))
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.symbol == "NVDA" and sig.bias == "long"
    assert sig.entry == 106.0
    assert sig.stop == 106.0 * 0.995
    assert sig.tp == 106.0 * 1.0075


def test_short_breakout():
    s = OpeningRangeBreakout(CFG)
    s.update({"TSLA": {"last": 250.0}}, _t(18, 30))
    sigs = s.update({"TSLA": {"last": 248.0}}, _t(19, 0))
    assert len(sigs) == 1 and sigs[0].bias == "short"
    assert sigs[0].stop == 248.0 * 1.005
    assert sigs[0].tp == 248.0 * 0.9925


def test_one_signal_per_symbol_per_session():
    s = OpeningRangeBreakout(CFG)
    s.update({"NVDA": {"last": 100.0}}, _t(18, 35))
    s.update({"NVDA": {"last": 105.0}}, _t(18, 40))
    assert len(s.update({"NVDA": {"last": 106.0}}, _t(19, 0))) == 1
    assert s.update({"NVDA": {"last": 109.0}}, _t(19, 30)) == []


def test_no_signal_inside_range():
    s = OpeningRangeBreakout(CFG)
    assert s.update({"NVDA": {"last": 100.0}}, _t(18, 35)) == []
    assert s.update({"NVDA": {"last": 101.0}}, _t(18, 40)) == []


def test_session_reset_between_days():
    s = OpeningRangeBreakout(CFG)
    s.update({"NVDA": {"last": 100.0}}, _t(18, 35))
    s.update({"NVDA": {"last": 105.0}}, _t(18, 40))
    assert len(s.update({"NVDA": {"last": 106.0}}, _t(19, 0))) == 1
    s.update({"NVDA": {"last": 100.0}}, datetime(2026, 8, 25, 18, 35))
    sigs = s.update({"NVDA": {"last": 98.0}}, datetime(2026, 8, 25, 19, 0))
    assert len(sigs) == 1 and sigs[0].bias == "short"


def test_ignores_bad_quotes():
    s = OpeningRangeBreakout(CFG)
    assert s.update({"NVDA": {"last": None}}, _t(19, 0)) == []
    assert s.update({"NVDA": {}}, _t(19, 0)) == []
    assert s.update({"NVDA": {"last": 0}}, _t(19, 0)) == []
    s.update({"NVDA": {"last": 100.0}}, _t(18, 40))
    sigs = s.update({"NVDA": {"last": 102.0}}, _t(19, 0))
    assert len(sigs) == 1 and sigs[0].symbol == "NVDA"