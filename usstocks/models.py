"""Domain models for the usstocks signal-only subsystem (ТЗ §5, Stage C).

All timestamps are timezone-aware datetimes (America/New_York for session
math, UTC internally acceptable as long as tz is attached). Nothing in this
package may carry an order to a broker: the only executor is
execution.DisabledExecutor.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

STRATEGY_VERSION = "vwap_pullback_continuation-v1"

_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


def validate_symbol(symbol: str) -> str:
    """Validate and normalize ticker symbol (e.g. 'AAPL', 'BRK.B')."""
    if not isinstance(symbol, str):
        raise ValueError(f"Symbol must be string, got {type(symbol).__name__}")
    s = symbol.strip().upper()
    if not s or not _SYMBOL_RE.match(s):
        raise ValueError(f"Invalid symbol format: {symbol!r}")
    return s


@dataclass
class Bar:
    """One CLOSED OHLCV candle."""

    ts: datetime            # bar OPEN time, tz-aware
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.ts is None or not isinstance(self.ts, datetime) or self.ts.tzinfo is None:
            raise ValueError(f"Bar.ts must be timezone-aware datetime, got {self.ts!r}")
        if self.open < 0 or self.high < 0 or self.low < 0 or self.close < 0:
            raise ValueError("Bar prices must be non-negative")
        if self.volume < 0:
            raise ValueError("Bar volume must be non-negative")

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

    def __post_init__(self) -> None:
        self.symbol = validate_symbol(self.symbol)


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

    def __post_init__(self) -> None:
        self.symbol = validate_symbol(self.symbol)
        if self.side not in ("long", "short"):
            raise ValueError(f"TradeSignal.side must be 'long' or 'short', got {self.side!r}")


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
    has_partial_fill: bool = False
    last_loss_time: Optional[datetime] = None  # in-memory only; resets on restart


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
