"""Tests for challenge limits: daily -$30 floating, overall -$90 floating, buying power, reset."""

from datetime import datetime, timezone, timedelta

import pytest

from execution.stealth.humanized_risk_manager import HumanizedRiskManager
from execution.stealth.config import StealthConfig


def test_daily_hard_stop_floating():
    config = StealthConfig(seed=42, challenge_daily_hard_stop=30.0, challenge_overall_buffer=10.0)
    rm = HumanizedRiskManager(seed=42, config=config)

    # Simulate floating PnL -20, closed 0 => daily -20, should allow trade
    rm.update_floating_pnl(-20, equity=980, now=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc))
    can, reason = rm.can_trade()
    assert can is True

    # Floating -25 + closed -10 = -35 daily => should block at -$30 hard stop
    rm._closed_pnl_since_reset = -10
    rm.update_floating_pnl(-25, equity=965, now=datetime(2026, 8, 20, 10, 5, tzinfo=timezone.utc))
    assert rm.get_daily_pnl() == -35
    can, reason = rm.can_trade()
    assert can is False
    assert "daily hard stop" in reason.lower()


def test_overall_buffer_floating():
    config = StealthConfig(seed=42, challenge_max_overall_loss=100.0, challenge_overall_buffer=10.0)
    rm = HumanizedRiskManager(seed=42, config=config)

    # Overall PnL -85 should allow (buffer at -90)
    rm._overall_pnl = -85
    can, reason = rm.can_trade()
    assert can is True

    # Overall -95 should block (buffer hit at -90)
    rm._overall_pnl = -95
    can, reason = rm.can_trade()
    assert can is False
    assert "overall" in reason.lower()


def test_force_close_trigger():
    config = StealthConfig(seed=42)
    rm = HumanizedRiskManager(seed=42, config=config)

    # Daily -30 should trigger force close
    rm._daily_pnl = -30
    rm._overall_pnl = -20
    should, reason = rm.should_force_close(daily_pnl=-30, overall_pnl=-20)
    assert should is True

    # Overall -90 should trigger force close
    should, reason = rm.should_force_close(daily_pnl=-10, overall_pnl=-90)
    assert should is True

    # Early warning within $5 of hard stop
    should, reason = rm.should_force_close(daily_pnl=-26, overall_pnl=-20)
    assert should is True
    assert "early warning" in reason.lower()

    # Normal -10 should not force close
    should, reason = rm.should_force_close(daily_pnl=-10, overall_pnl=-20)
    assert should is False


def test_buying_power_check():
    from challenge.risk import ChallengeRisk

    cfg = {"risk": {"per_trade_risk_usd": 10, "max_leverage": 5}}
    risk = ChallengeRisk(cfg)

    equity = 1000
    entry = 200
    stop = 195  # SL $5
    risk_usd = 10
    shares = risk.position_size_from_stop(entry, stop, risk_usd, equity)
    # $10 / $5 = 2 shares
    assert shares == 2
    # Notional $400, buying power $5000, should be ok
    buying_power = equity * 5
    notional = entry * shares
    assert notional <= buying_power

    # Expensive stock, same SL $5
    entry = 500
    stop_exp = 495  # SL $5
    shares = risk.position_size_from_stop(entry, stop_exp, risk_usd, equity)
    # $10 / $5 =2, notional $1000 <= $5000 ok
    assert shares == 2

    # If risk would require 100 shares of $200 stock = $20000 notional > $5000, should be capped
    entry = 200
    risk_usd = 500  # large risk would want 100 shares
    shares = risk.position_size_from_stop(entry, stop, risk_usd, equity)
    # 500/5=100 shares, notional $20000 > $5000, capped to 25 shares (5000/200)
    assert shares == 25


def test_daily_reset_utc4_window():
    config = StealthConfig(seed=42, challenge_daily_reset_window_utc4=("00:00", "00:13"), challenge_daily_reset_offset_hours=4)
    rm = HumanizedRiskManager(seed=42, config=config)

    # 00:05 UTC+4 = 20:05 UTC previous day
    # In UTC: 20:05 UTC should be in reset window
    now_utc = datetime(2026, 8, 20, 20, 5, tzinfo=timezone.utc)
    assert rm._is_in_reset_window(now_utc) is True

    # 00:20 UTC+4 = 20:20 UTC -> outside window
    now_utc_out = datetime(2026, 8, 20, 20, 20, tzinfo=timezone.utc)
    assert rm._is_in_reset_window(now_utc_out) is False

    # 10:00 UTC = 14:00 UTC+4 -> outside
    now_utc_day = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    assert rm._is_in_reset_window(now_utc_day) is False


def test_daily_reset_resets_state():
    config = StealthConfig(seed=42)
    rm = HumanizedRiskManager(seed=42, config=config)

    now = datetime(2026, 8, 20, 20, 5, tzinfo=timezone.utc)  # in reset window
    rm._overall_pnl = -20
    rm._floating_pnl = -5
    rm._closed_pnl_since_reset = -10
    rm._daily_pnl = -15
    rm._daily_hard_stopped = True
    rm._last_reset_date = None

    rm._ensure_day(now)
    # After reset window, daily should be reset, overall should persist
    assert rm._daily_pnl == rm._floating_pnl  # daily = floating after reset
    assert rm._closed_pnl_since_reset == 0.0
    assert rm._daily_hard_stopped is False
    assert rm._overall_pnl == -20  # overall persists


def test_shares_jitter():
    config = StealthConfig(seed=42)
    rm = HumanizedRiskManager(seed=42, config=config)
    base = 10
    shares_list = [rm.get_share_size(base) for _ in range(1000)]
    jittered = [s for s in shares_list if s != base]
    # 15% jitter
    assert 80 <= len(jittered) <= 250
    for s in jittered:
        assert s in [9, 11]


def test_risk_jitter_range():
    config = StealthConfig(seed=42, risk_jitter_range=(0.007, 0.013))
    rm = HumanizedRiskManager(seed=42, config=config)
    risks = [rm.get_risk_pct() for _ in range(1000)]
    # Majority should be in 0.007-0.013 (0.7-1.3%)
    in_range = sum(1 for r in risks if 0.007 <= r <= 0.013)
    assert in_range >= 850
