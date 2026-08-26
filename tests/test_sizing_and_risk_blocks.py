# -*- coding: utf-8 -*-
"""ТЗ §12.8-13: sizing caps and every risk-engine block."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from usstocks.models import RiskState
from usstocks.risk_engine import RiskEngine, RiskDecision
from usstocks.sizing import size_position, targets_from_r

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 26, 10, 0, tzinfo=NY)


def _state(**kw) -> RiskState:
    base = dict(session_date="2026-08-26", realized_pnl_usd=0.0,
                unrealized_pnl_usd=0.0, trades_taken=0, consecutive_losses=0,
                active_symbol=None, day_stopped=False)
    base.update(kw)
    return RiskState(**base)


def _engine() -> RiskEngine:
    return RiskEngine(personal_daily_stop_usd=-20.0, max_trades_per_day=2,
                      max_consecutive_losses=2, daily_profit_lock_usd=20.0,
                      no_new_entries_minutes_before_close=25.0)


def _allow(state, close_in_min=120):
    d = _engine().evaluate(state, NOW, NOW + timedelta(minutes=close_in_min))
    return d


# ---------------------------------------------------------------------------
# Sizing (ТЗ §8): stop first, then shares.
# ---------------------------------------------------------------------------

def test_sizing_capped_by_10usd_risk():
    r = size_position(entry=201.25, stop=200.70)
    assert r.ok
    assert r.shares == 18                       # floor(10 / 0.55)
    assert r.actual_risk_usd <= 10.0
    assert r.notional_usd == pytest.approx(18 * 201.25)


def test_sizing_capped_by_5000_notional():
    r = size_position(entry=900.00, stop=899.50)
    assert r.ok
    assert r.shares == 5                        # floor(5000/900), not risk cap
    assert r.notional_usd <= 5000


def test_sizing_zero_shares_when_stop_too_wide():
    r = size_position(entry=100.00, stop=89.00)
    assert not r.ok and r.reason == "RISK_CAP_ZERO_SHARES"


def test_sizing_zero_shares_when_price_too_high_for_notional():
    r = size_position(entry=6000.00, stop=5999.00)
    assert not r.ok and r.reason == "NOTIONAL_CAP_ZERO_SHARES"


def test_sizing_rejects_crossed_levels():
    assert not size_position(100, 100).ok
    assert not size_position(0, -1).ok


def test_targets_from_r_long_and_short():
    tp1, tp2 = targets_from_r("long", 100.0, 99.0)
    assert (tp1, tp2) == (101.0, 102.0)
    tp1, tp2 = targets_from_r("short", 100.0, 101.0)
    assert (tp1, tp2) == (99.0, 98.0)


# ---------------------------------------------------------------------------
# Risk blocks (ТЗ §8) — deterministic order, all reasons logged via code.
# ---------------------------------------------------------------------------

def test_allow_on_clean_state():
    d = _allow(_state())
    assert d.allowed and d.code == "ALLOW"


def test_personal_daily_stop_blocks():
    d = _allow(_state(realized_pnl_usd=-15.0, unrealized_pnl_usd=-5.5))
    assert not d.allowed and d.code == "PERSONAL_DAILY_STOP"


def test_max_trades_reached_blocks():
    d = _allow(_state(trades_taken=2))
    assert not d.allowed and d.code == "MAX_TRADES_REACHED"


def test_two_consecutive_losses_block_signals():
    d = _allow(_state(consecutive_losses=2))
    assert not d.allowed and d.code == "MAX_CONSECUTIVE_LOSSES"
    # one loss still allows (threshold is >=2)
    d1 = _allow(_state(consecutive_losses=1))
    assert d1.allowed


def test_profit_lock_blocks_after_plus_20():
    d = _allow(_state(realized_pnl_usd=20.0))
    assert not d.allowed and d.code == "DAILY_PROFIT_LOCK"


def test_session_close_guard_blocks_within_25_minutes():
    d = _allow(_state(), close_in_min=24)
    assert not d.allowed and d.code == "SESSION_CLOSE_GUARD"
    ok = _allow(_state(), close_in_min=26)
    assert ok.allowed


def test_active_position_blocks_new_signal():
    d = _allow(_state(active_symbol="AMD"))
    assert not d.allowed and d.code == "ACTIVE_POSITION_EXISTS"


def test_operator_day_stopped_blocks_everything():
    d = _allow(_state(day_stopped=True))
    assert not d.allowed and d.code == "DAY_STOPPED"


def test_engine_reads_profile_numbers():
    eng = RiskEngine.from_cfg({"risk": {"max_trades_per_day": 7}})
    assert eng.max_trades_per_day == 7
