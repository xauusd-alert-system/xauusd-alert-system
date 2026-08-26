"""Domain models for the usstocks signal-only subsystem (ТЗ §5, Stage C).

All timestamps are timezone-aware datetimes (America/New_York for session
math, UTC internally acceptable as long as tz is attached). Nothing in this
package may carry an order to a broker: the only executor is
execution.DisabledExecutor.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

STRATEGY_VERSION = "vwap_pullback_continuation-v1"


@dataclass
class Bar:
    """One CLOSED OHLCV candle."""

    ts: datetime            # bar OPEN time, tz-aware
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass
class PremarketSnapshot:
    """Premarket ranking input for one symbol (ТЗ §7.1)."""

    symbol: str
    price: float
    prev_close: float
    gap_pct: float                 # signed, % vs prev close
    relative_volume: float         # premarket vol / avg premarket vol
    avg_daily_dollar_volume: float
    spread_pct: float
    fresh_news_catalyst: bool = False
    score: int = 0


@dataclass
class TradeSignal:
    """A complete, manually-executable plan — never sent anywhere automatic."""

    symbol: str
    side: str                      # "long" | "short"
    entry_low: float               # trigger zone (structure break ± buffer)
    entry_high: float
    stop: float
    tp1: float
    tp2: float
    risk_per_share: float
    shares: int
    notional_usd: float
    planned_risk_usd: float
    grade: str                     # A+/A/B/... from quality score (advisory)
    passed_checks: List[str] = field(default_factory=list)
    why: List[str] = field(default_factory=list)     # human bullets for TG
    metrics: Dict[str, float] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    signal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    strategy_version: str = STRATEGY_VERSION


@dataclass
class RiskState:
    """Daily challenge state consumed by RiskEngine (ТЗ §8)."""

    session_date: str                       # YYYY-MM-DD (NY)
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    trades_taken: int = 0
    consecutive_losses: int = 0
    active_symbol: Optional[str] = None     # open position OR pending accepted signal
    day_stopped: bool = False


@dataclass
class RiskEvent:
    ts: datetime
    code: str          # ALLOW | PERSONAL_DAILY_STOP | MAX_TRADES_REACHED | ...
    allowed: bool
    reason: str
    symbol: Optional[str] = None


@dataclass
class WatchlistItem:
    snapshot: PremarketSnapshot
    is_tech: bool       # benchmark choice per ТЗ §7.3.5 (QQQ tech / SPY other)
