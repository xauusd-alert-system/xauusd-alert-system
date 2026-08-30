"""Tests for execution/trade_throttle.py — TradeThrottle module.

Covers:
  - Daily trade limit blocking
  - Cooldown activation and expiry
  - Hard stop streak
  - Risk step-down multiplier
  - Daily loss limit circuit breaker
  - Day reset at UTC midnight
  - State persistence across restarts
"""

import json
import os
import tempfile
import time
from datetime import UTC, datetime, timezone

import pytest

from execution.trade_throttle import TradeThrottle


def _cfg(**overrides):
    base = {
        "risk_throttle": {
            "max_trades_per_day": 5,
            "loss_streak_threshold": 2,
            "cooldown_minutes": 45,
            "hard_stop_streak": 3,
            "risk_step_down_map": {1: 1.0, 2: 0.5, 3: 0.25},
            "max_daily_loss_pct": 3.0,
            "reset_on_utc_midnight": True,
        }
    }
    if overrides:
        base["risk_throttle"].update(overrides)
    return base


@pytest.fixture()
def throttle(tmp_path):
    """Fresh TradeThrottle with temp state file."""
    state_path = str(tmp_path / "throttle_state.json")
    return TradeThrottle(_cfg(), state_path=state_path)


@pytest.fixture()
def throttle_short_cooldown(tmp_path):
    """TradeThrottle with very short cooldown for expiry tests."""
    state_path = str(tmp_path / "throttle_state.json")
    return TradeThrottle(_cfg(cooldown_minutes=0.01), state_path=state_path)  # 0.6s


# ---- Daily trade limit ----


def test_allows_up_to_max_trades(throttle):
    for i in range(5):
        ok, reason = throttle.can_trade(10000.0)
        assert ok, f"trade {i + 1} should be allowed: {reason}"
        throttle.on_trade_closed(10.0)  # winning trade
    ok, reason = (
        throttle.on_trade_closed.__wrapped__(10.0)
        if hasattr(throttle.on_trade_closed, "__wrapped__")
        else throttle.can_trade(10000.0)
    )
    # After 5 trades + 1 more attempt
    ok, reason = throttle.can_trade(10000.0)
    assert not ok
    assert "daily_limit_reached" in reason


def test_daily_limit_blocks_after_max(throttle):
    for _ in range(5):
        throttle.on_trade_closed(10.0)
    ok, reason = throttle.can_trade(10000.0)
    assert not ok
    assert "daily_limit_reached" in reason
    assert "5/5" in reason


# ---- Cooldown after loss streak ----


def test_cooldown_activates_after_threshold(throttle):
    throttle.on_trade_closed(-10.0)  # loss 1
    ok, _ = throttle.can_trade(10000.0)
    assert ok  # 1 loss < threshold (2)

    throttle.on_trade_closed(-10.0)  # loss 2
    ok, reason = throttle.can_trade(10000.0)
    assert not ok
    assert "cooldown_active" in reason


def test_cooldown_resets_on_win(throttle):
    throttle.on_trade_closed(-10.0)
    throttle.on_trade_closed(-10.0)
    ok, _ = throttle.can_trade(10000.0)
    assert not ok  # cooldown active

    # Simulate win by setting cooldown_until in the past
    throttle.cooldown_until = 0.0
    throttle.consecutive_losses = 0
    ok, _ = throttle.can_trade(10000.0)
    assert ok


def test_cooldown_expires(throttle):
    throttle.on_trade_closed(-10.0)
    throttle.on_trade_closed(-10.0)
    ok, _ = throttle.can_trade(10000.0)
    assert not ok  # cooldown active

    # Force cooldown to expire by setting it in the past
    throttle.cooldown_until = time.time() - 1.0
    ok, _ = throttle.can_trade(10000.0)
    assert ok


# ---- Hard stop streak ----


def test_hard_stop_at_critical_streak(throttle):
    for _ in range(3):
        throttle.on_trade_closed(-10.0)
    ok, reason = throttle.can_trade(10000.0)
    assert not ok
    assert "hard_stop_streak" in reason


def test_hard_stop_persists_all_day(throttle):
    for _ in range(3):
        throttle.on_trade_closed(-10.0)
    ok, _ = throttle.can_trade(10000.0)
    assert not ok

    # Even after cooldown would have expired
    throttle.cooldown_until = 0.0
    ok, reason = throttle.can_trade(10000.0)
    assert not ok
    assert "hard_stop" in reason


# ---- Risk step-down ----


def test_risk_multiplier_no_losses(throttle):
    assert throttle.risk_multiplier() == 1.0


def test_risk_multiplier_after_one_loss(throttle):
    throttle.on_trade_closed(-10.0)
    assert throttle.risk_multiplier() == 1.0


def test_risk_multiplier_after_two_losses(throttle):
    throttle.on_trade_closed(-10.0)
    throttle.on_trade_closed(-10.0)
    assert throttle.risk_multiplier() == 0.5


def test_risk_multiplier_after_three_losses(throttle):
    for _ in range(3):
        throttle.on_trade_closed(-10.0)
    assert throttle.risk_multiplier() == 0.25


def test_risk_multiplier_resets_on_win(throttle):
    throttle.on_trade_closed(-10.0)
    throttle.on_trade_closed(-10.0)
    assert throttle.risk_multiplier() == 0.5

    throttle.on_trade_closed(10.0)  # win
    assert throttle.risk_multiplier() == 1.0


# ---- Daily loss limit ----


def test_daily_loss_limit_blocks(throttle):
    # Start at 10000, lose 3% = $300
    ok, _ = throttle.can_trade(10000.0)
    assert ok  # establish starting equity

    ok, reason = throttle.can_trade(9699.0)  # -3.01%
    assert not ok
    assert "daily_loss_limit" in reason


def test_daily_loss_limit_sets_hard_stop(throttle):
    throttle.can_trade(10000.0)
    throttle.can_trade(9699.0)
    assert throttle.hard_stopped is True


# ---- Day reset ----


def test_day_reset_clears_counters(throttle):
    throttle.can_trade(10000.0)
    for _ in range(3):
        throttle.on_trade_closed(-10.0)
    assert throttle.trades_today == 3
    assert throttle.hard_stopped is True

    # Simulate new day by changing current_day
    throttle.current_day = (datetime.now(UTC) - __import__("datetime").timedelta(days=2)).date()
    throttle.can_trade(10000.0)  # triggers reset

    assert throttle.trades_today == 0
    assert throttle.hard_stopped is False
    # Audit 2026-08-23 A: the streak MUST reset with the session. The old
    # behavior (kept across days) meant 3 losses late Monday made Tuesday's
    # first loss an instant hard stop and held risk at 0.25x until a win.
    assert throttle.consecutive_losses == 0


# ---- State persistence ----


def test_state_persists_across_instances(tmp_path):
    state_path = str(tmp_path / "state.json")

    t1 = TradeThrottle(_cfg(), state_path=state_path)
    t1.can_trade(10000.0)
    t1.on_trade_closed(-10.0)
    t1.on_trade_closed(-10.0)

    # New instance loads state
    t2 = TradeThrottle(_cfg(), state_path=state_path)
    assert t2.trades_today == 2
    assert t2.consecutive_losses == 2
    assert t2.risk_multiplier() == 0.5


def test_state_file_is_valid_json(tmp_path):
    state_path = str(tmp_path / "state.json")
    t = TradeThrottle(_cfg(), state_path=state_path)
    t.can_trade(10000.0)
    t.on_trade_closed(-10.0)

    with open(state_path) as f:
        data = json.load(f)
    assert "current_day" in data
    assert "trades_today" in data
    assert "consecutive_losses" in data


# ---- get_state() ----


def test_get_state_snapshot(throttle):
    throttle.can_trade(10000.0)
    throttle.on_trade_closed(-10.0)
    state = throttle.get_state()
    assert state["trades_today"] == 1
    assert state["consecutive_losses"] == 1
    assert state["risk_multiplier"] == 1.0
    assert state["hard_stopped"] is False


# ---- Edge cases ----


def test_no_config_uses_defaults(tmp_path):
    state_path = str(tmp_path / "state.json")
    t = TradeThrottle({}, state_path=state_path)
    assert t.max_trades_per_day == 5
    assert t.cooldown_minutes == 45
    assert t.hard_stop_streak == 3


def test_pnl_exactly_zero_counts_as_win(throttle):
    throttle.on_trade_closed(-10.0)
    throttle.on_trade_closed(-10.0)
    assert throttle.consecutive_losses == 2

    throttle.on_trade_closed(0.0)  # breakeven
    assert throttle.consecutive_losses == 0


def test_multiple_restarts_preserve_state(tmp_path):
    state_path = str(tmp_path / "state.json")

    for _ in range(3):
        t = TradeThrottle(_cfg(), state_path=state_path)
        t.can_trade(10000.0)
        t.on_trade_closed(-10.0)

    t_final = TradeThrottle(_cfg(), state_path=state_path)
    assert t_final.trades_today == 3
    assert t_final.consecutive_losses == 3
    assert t_final.risk_multiplier() == 0.25


# ---- Stub mode (demo testing, 2026-08-28) ----


def test_stub_disables_loss_gates(tmp_path):
    """Stub: losses never trigger cooldown / hard stop / risk step-down."""
    state_path = str(tmp_path / "throttle_state.json")
    throttle = TradeThrottle(_cfg(stub=True), state_path=state_path)
    for _ in range(4):
        ok, reason = throttle.can_trade(10000.0)
        assert ok, f"stub should keep allowing trades: {reason}"
        throttle.on_trade_closed(-50.0)  # 4 consecutive losses
    ok, reason = throttle.can_trade(10000.0)
    assert ok, f"stub should not block after losses: {reason}"
    assert throttle.risk_multiplier() == 1.0
    snap = throttle.get_state()
    assert not snap["cooldown_active"]
    assert not snap["hard_stopped"]
    assert snap["risk_multiplier"] == 1.0


def test_stub_still_enforces_daily_cap(tmp_path):
    """Stub keeps the daily trade-count cap (non-loss gate)."""
    state_path = str(tmp_path / "throttle_state.json")
    throttle = TradeThrottle(_cfg(stub=True), state_path=state_path)
    for _ in range(5):
        throttle.on_trade_closed(-50.0)
    ok, reason = throttle.can_trade(10000.0)
    assert not ok
    assert "daily_limit_reached" in reason
