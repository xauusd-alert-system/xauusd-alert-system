"""Backwards-compatible wrappers (ТЗ 8.5 transition shims).

Responsibility:
    ``InstitutionalRiskManager`` is the historical public face of the daily
    risk gates. After the ТЗ 8.5 refactor the implementation lives in
    ``risk/limits.RiskLimits`` (+ ``risk.state.RiskState``); this module keeps
    the old class name and the old attribute surface so existing callers and
    tests keep working unchanged:

    - legacy attributes ``starting_equity_today`` / ``starting_balance_today``
      / ``daily_trades_count`` / ``circuit_breaker_tripped`` / ``current_day``
      are delegated to the embedded ``RiskState``;
    - ``can_trade(asset_key, groups_by_asset, singles_by_asset)`` semantics
      are unchanged (group-aware counting, P0-5 swap-excluding breaker).

    This module is transitional — it will be deleted together with the
    execution shims in the cleanup phase (Фаза 7).

Example::

    from risk.compat import InstitutionalRiskManager
    mgr = InstitutionalRiskManager(cfg, magic=777111,
                                   state_path="logs/risk_state.json")
    ok, reason = mgr.can_trade("XAUUSD")
"""
from __future__ import annotations

from risk.limits import RiskLimits
from risk.state import RiskState

# Legacy attribute names delegated to the embedded RiskState.
_STATE_ATTRS = (
    "current_day",
    "starting_equity_today",
    "starting_balance_today",
    "hwm",
    "daily_trades_count",
    "circuit_breaker_tripped",
)


class InstitutionalRiskManager(RiskLimits):
    """Historical API name for the daily risk gates (thin compat wrapper
    around :class:`risk.limits.RiskLimits`; see module docstring)."""

    def __init__(self, cfg: dict, magic: int = None,
                 state_path: str = "logs/risk_state.json"):
        state = RiskState(state_path)
        super().__init__(cfg, magic=magic, state=state)
        self.state_path = state_path

    def __getattr__(self, name):
        # Only called for attributes NOT found normally: delegate the legacy
        # state surface to the embedded RiskState.
        if name in _STATE_ATTRS:
            return getattr(self.state, name)
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}")
