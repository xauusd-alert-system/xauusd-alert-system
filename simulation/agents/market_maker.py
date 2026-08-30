"""
Inventory-averse market maker.

Quotes both sides of the book around the current mid price with a configurable
spread offset. When inventory grows beyond +/- mm_max_inventory it skews its
quotes (or crosses) to mean-revert inventory toward zero.
"""

from __future__ import annotations

from typing import Optional

from simulation.agents.base_agent import BaseAgent
from simulation.engine.order import Order


class MarketMaker(BaseAgent):
    """Passive liquidity provider with simple inventory skew."""

    def __init__(self, agent_id: str, cfg: dict, rng=None) -> None:
        super().__init__(agent_id, cfg, rng)
        self.spread_offset_pct: float = float(cfg.get("mm_spread_offset_pct", 0.0003))
        self.max_inventory: float = float(cfg.get("mm_max_inventory", 100.0))
        self.quote_volume: float = 1.0

    def act(self, context: dict, tick: int) -> Optional[Order]:
        mid = context.get("mid")
        if mid is None:
            return None

        ask = mid + mid * self.spread_offset_pct
        bid = mid - mid * self.spread_offset_pct

        inventory = float(context.get("inventory", 0.0))

        # Inventory control: push price away to shed unwanted inventory.
        if inventory > self.max_inventory:
            ask = mid * (1.0 - self.spread_offset_pct)
        elif inventory < -self.max_inventory:
            bid = mid * (1.0 + self.spread_offset_pct)

        side = "SELL" if self.rng.random() < 0.5 else "BUY"
        price = ask if side == "SELL" else bid

        return Order(
            agent_id=self.agent_id,
            side=side,
            order_type="LIMIT",
            price=round(price, 6),
            volume=self.quote_volume,
            tick=tick,
        )
