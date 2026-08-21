"""
Order and Trade domain models for the virtual limit order book simulation.

- Order: a single order submitted by an agent or by the broker shim.
- Trade: a matched execution between a buy order and a sell order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class Order:
    """A single order in the simulated limit order book."""

    agent_id: str
    side: str            # "BUY" | "SELL"
    order_type: str      # "LIMIT" | "MARKET" | "CANCEL"
    price: float | None
    volume: float
    tick: int
    order_id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"

    @property
    def is_sell(self) -> bool:
        return self.side == "SELL"


@dataclass
class Trade:
    """A matched execution between a buy order and a sell order."""

    trade_id: str
    buy_order_id: str
    sell_order_id: str
    price: float
    volume: float
    tick: int
