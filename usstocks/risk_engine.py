"""Risk engine — hard admission blocks for the challenge day (ТЗ §8).

The engine NEVER sizes or routes; it only allows/denies a new signal and
records the reason. Denials are cheap to evaluate and must be logged by the
caller (Telegram throttling is the runner's concern, Stage E).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from usstocks.indicators import minutes_until
from usstocks.models import RiskState
from shared.risk_protocol import RiskDecision, RiskEngineProtocol


class RiskEngine:
    """Config-driven blocks; numbers come from config/us_stocks_challenge.yaml."""

    def __init__(self, *, personal_daily_stop_usd: float = -20.0,
                 max_trades_per_day: int = 2,
                 max_consecutive_losses: int = 2,
                 daily_profit_lock_usd: float = 20.0,
                 no_new_entries_minutes_before_close: float = 25.0):
        self.personal_daily_stop_usd = personal_daily_stop_usd
        self.max_trades_per_day = max_trades_per_day
        self.max_consecutive_losses = max_consecutive_losses
        self.daily_profit_lock_usd = daily_profit_lock_usd
        self.no_new_entries_minutes_before_close = no_new_entries_minutes_before_close

    @classmethod
    def from_cfg(cls, cfg: dict) -> "RiskEngine":
        risk = (cfg or {}).get("risk", {})
        return cls(
            personal_daily_stop_usd=float(risk.get("personal_daily_stop_usd", -20.0)),
            max_trades_per_day=int(risk.get("max_trades_per_day", 2)),
            max_consecutive_losses=int(risk.get("max_consecutive_losses", 2)),
            daily_profit_lock_usd=float(risk.get("daily_profit_lock_usd", 20.0)),
            no_new_entries_minutes_before_close=float(
                risk.get("no_new_entries_minutes_before_close", 25)),
        )

    def evaluate(self, state: RiskState, now: datetime,
                 session_close_at: datetime,
                 symbol: Optional[str] = None) -> RiskDecision:
        """Return the first blocking rule (deterministic order below)."""
        total_pnl = state.realized_pnl_usd + state.unrealized_pnl_usd

        if state.day_stopped:
            return RiskDecision(False, "DAY_STOPPED",
                                "operator pressed stop-day")
        if getattr(state, "has_partial_fill", False):
            return RiskDecision(
                False, "PARTIAL_FILL_ACTIVE",
                f"partial fill active on {state.active_symbol or 'position'}, new entries blocked")
        if total_pnl <= self.personal_daily_stop_usd:
            return RiskDecision(
                False, "PERSONAL_DAILY_STOP",
                f"realized+unrealized {total_pnl:+.2f} <= "
                f"{self.personal_daily_stop_usd:+.2f}")
        if state.trades_taken >= self.max_trades_per_day:
            return RiskDecision(
                False, "MAX_TRADES_REACHED",
                f"{state.trades_taken}/{self.max_trades_per_day} trades today")
        if state.consecutive_losses >= self.max_consecutive_losses:
            return RiskDecision(
                False, "MAX_CONSECUTIVE_LOSSES",
                f"{state.consecutive_losses} losses in a row")
        if state.realized_pnl_usd >= self.daily_profit_lock_usd:
            return RiskDecision(
                False, "DAILY_PROFIT_LOCK",
                f"day target locked at {state.realized_pnl_usd:+.2f}")
        if state.active_symbol:
            return RiskDecision(
                False, "ACTIVE_POSITION_EXISTS",
                f"position/signal already active for {state.active_symbol}")
        mins_left = minutes_until(session_close_at, now)
        if mins_left <= self.no_new_entries_minutes_before_close:
            return RiskDecision(
                False, "SESSION_CLOSE_GUARD",
                f"{mins_left:.0f} min to close <= "
                f"{self.no_new_entries_minutes_before_close:.0f} guard window")
        return RiskDecision(True, "ALLOW", "all challenge gates passed")
