"""Tests for risk/throttle.py — rate-based throttling only (P2-10).

Covers:
  - sliding window admits up to max_orders_per_minute;
  - blocked requests carry a wait hint;
  - the window clears as stamps age out;
  - P2-10: no daily-limit knowledge (guard duplicated at unit level).
"""

import time

from risk.throttle import RateThrottle


def test_admits_up_to_rate():
    rt = RateThrottle(max_orders_per_minute=3)
    for _ in range(3):
        ok, _ = rt.can_trade("XAUUSD")
        assert ok
        rt.record_order("XAUUSD")
    ok, reason = rt.can_trade("XAUUSD")
    assert not ok
    assert "rate_throttled" in reason
    assert "wait" in reason


def test_per_asset_isolation():
    rt = RateThrottle(max_orders_per_minute=1)
    rt.record_order("XAUUSD")
    ok, _ = rt.can_trade("XAUUSD")
    assert not ok
    ok, _ = rt.can_trade("XAGUSD")  # other asset unaffected
    assert ok


def test_window_ages_out():
    rt = RateThrottle({"risk_throttle": {"rate_window_seconds": 0.05}}, max_orders_per_minute=1)
    rt.record_order("XAUUSD")
    ok, _ = rt.can_trade("XAUUSD")
    assert not ok
    time.sleep(0.08)
    ok, _ = rt.can_trade("XAUUSD")
    assert ok


def test_clear_resets_window():
    rt = RateThrottle(max_orders_per_minute=1)
    rt.record_order("XAUUSD")
    rt.clear("XAUUSD")
    ok, _ = rt.can_trade("XAUUSD")
    assert ok


def test_config_driven_limit():
    rt = RateThrottle({"risk_throttle": {"max_orders_per_minute": 7}})
    assert rt.max_orders_per_minute == 7


def test_no_daily_limits_in_throttle():
    """P2-10 guard (unit level): the throttle module has no daily-limit
    state or methods — daily limits live ONLY in risk/limits.py."""
    rt = RateThrottle()
    for forbidden in (
        "max_trades_per_day",
        "trades_today",
        "daily_trades_count",
        "max_daily_loss_pct",
        "hard_stopped",
        "on_trade_closed",
    ):
        assert not hasattr(rt, forbidden), forbidden
