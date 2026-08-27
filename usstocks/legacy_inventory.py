"""Legacy modules inventory and isolation manifest (ТЗ §6, Stage B/F, P1-1).

This module defines the authoritative status of all legacy subsystems in the
codebase to guarantee that the signal-only `us_stocks_challenge` and `replay`
profiles remain 100% isolated from automated execution, browser manipulation,
or legacy MT5 brokers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class ModuleInventoryItem:
    name: str
    path: str
    category: str              # "execution" | "automation" | "stealth" | "legacy_analytics" | "shared"
    allowed_in_us_stocks: bool
    isolation_mechanism: str
    description: str


LEGACY_INVENTORY: Dict[str, ModuleInventoryItem] = {
    "mt5_trader": ModuleInventoryItem(
        name="MT5 Auto-Trader",
        path="execution/mt5_trader.py",
        category="execution",
        allowed_in_us_stocks=False,
        isolation_mechanism="guards.assert_auto_trading_allowed / scripts.run_bot interlock",
        description="Legacy MetaTrader 5 broker order execution loop.",
    ),
    "challenge_browser_runner": ModuleInventoryItem(
        name="Challenge Browser Runner",
        path="challenge/runner.py",
        category="automation",
        allowed_in_us_stocks=False,
        isolation_mechanism="guards.assert_auto_trading_allowed interlock on startup",
        description="Playwright browser bot clicking orders on HashHedge.",
    ),
    "stealth_bridge": ModuleInventoryItem(
        name="Stealth Runner Bridge",
        path="challenge/stealth/runner_bridge.py",
        category="stealth",
        allowed_in_us_stocks=False,
        isolation_mechanism="runner_bridge.build_engine() interlock",
        description="Stealth keyboard/mouse humanizer and timing simulation.",
    ),
    "legacy_control_bot": ModuleInventoryItem(
        name="Legacy MT5 Control Bot",
        path="alerts/control_bot.py",
        category="execution",
        allowed_in_us_stocks=False,
        isolation_mechanism="Not launched under us_stocks_challenge (owned by usstocks.bot)",
        description="Telegram bot with /closeall and MT5 position management.",
    ),
    "pairs_analysis": ModuleInventoryItem(
        name="Pairs Analysis Suite",
        path="pairs_analysis/",
        category="legacy_analytics",
        allowed_in_us_stocks=False,
        isolation_mechanism="Isolated research modules; not imported in usstocks",
        description="Cointegration and statistical arbitrage analysis for FX/metals.",
    ),
    "realtime_dashboard": ModuleInventoryItem(
        name="Realtime Web Dashboard",
        path="realtime/",
        category="legacy_analytics",
        allowed_in_us_stocks=False,
        isolation_mechanism="Separate FastAPI service; not required for signal-only US scanner",
        description="Legacy WebSocket and order book feed dashboard.",
    ),
}


def get_blocked_modules_for_profile(profile: str) -> List[ModuleInventoryItem]:
    """Return all inventory items that must NOT run under the given profile."""
    if profile in ("us_stocks_challenge", "replay"):
        return [item for item in LEGACY_INVENTORY.values() if not item.allowed_in_us_stocks]
    return []


def is_module_allowed(module_key: str, profile: str) -> bool:
    """Check if a registered module is permitted under a specific profile."""
    item = LEGACY_INVENTORY.get(module_key)
    if item is None:
        return True
    if profile in ("us_stocks_challenge", "replay"):
        return item.allowed_in_us_stocks
    return True
