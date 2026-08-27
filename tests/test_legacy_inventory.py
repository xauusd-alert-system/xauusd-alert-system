"""Tests for legacy module inventory and isolation (P1-1)."""
from usstocks.legacy_inventory import (
    LEGACY_INVENTORY,
    get_blocked_modules_for_profile,
    is_module_allowed,
)


def test_legacy_inventory_contains_all_critical_modules():
    expected_keys = {
        "mt5_trader",
        "challenge_browser_runner",
        "stealth_bridge",
        "legacy_control_bot",
        "pairs_analysis",
        "realtime_dashboard",
    }
    assert expected_keys <= set(LEGACY_INVENTORY.keys())


def test_blocked_modules_for_us_stocks_challenge():
    blocked = get_blocked_modules_for_profile("us_stocks_challenge")
    assert len(blocked) >= 4
    names = {b.name for b in blocked}
    assert "MT5 Auto-Trader" in names
    assert "Challenge Browser Runner" in names
    assert "Stealth Runner Bridge" in names

    assert not is_module_allowed("mt5_trader", "us_stocks_challenge")
    assert not is_module_allowed("challenge_browser_runner", "us_stocks_challenge")
    assert not is_module_allowed("stealth_bridge", "us_stocks_challenge")


def test_legacy_modules_allowed_in_forex_profile():
    blocked = get_blocked_modules_for_profile("forex_legacy")
    assert len(blocked) == 0
    assert is_module_allowed("mt5_trader", "forex_legacy")
