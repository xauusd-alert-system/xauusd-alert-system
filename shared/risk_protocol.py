"""Unified Risk Engine Protocol and interface definitions (P2-1)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol


@dataclass
class RiskDecision:
    allowed: bool
    code: str
    reason: str


class RiskStateProtocol(Protocol):
    session_date: str
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    trades_taken: int
    consecutive_losses: int
    active_symbol: Optional[str]
    day_stopped: bool
    has_partial_fill: bool


class RiskEngineProtocol(Protocol):
    def evaluate(
        self,
        state: RiskStateProtocol,
        now: datetime,
        session_close_at: datetime,
        symbol: Optional[str] = None,
    ) -> RiskDecision:
        ...
