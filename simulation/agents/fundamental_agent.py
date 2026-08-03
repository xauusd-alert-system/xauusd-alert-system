"""
Fundamental / rebalancing agent.

Tracks a slow-moving 'fundamental' anchor near the initial price and trades
back toward it. It also reacts to news shocks (context["news_shock"]), adding
an impact factor to its directional pressure.
"""
from __future__ import annotations

from typing import Optional

from simulation.agents.base_agent import BaseAgent
from simulation.engine.order import Order


class FundamentalAgent(BaseAgent):
    """Slow rebalancer that anchors price around its fundamental value."""

    def __init__(self, agent_id: str, cfg: dict, rng=None) -> None:
        super().__init__(agent_id, cfg, rng)
        self.impact_factor: float = float(cfg.get("fundamental_impact_factor", 0.5))
        self.anchor: float = float(cfg.get("initial_price", 2400.0))
        self.volume: float = float(cfg.get("fundamental_volume", 1.0))
        self.band_pct: float = float(cfg.get("fundamental_band_pct", 0.0005))

    def act(self, context: dict, tick: int) -> Optional[Order]:
        mid = context.get("mid")
        if mid is None:
            return None

        # Deviation of market from the fundamental anchor.
        deviation = (mid - self.anchor) / self.anchor

        # Apply news-driven shock pressure.
        shock = context.get("news_shock")
        if shock is not None:
            deviation -= shock * self.impact_factor

        # Only trade when outside the deadband, closing toward the anchor.
        if deviation > self.band_pct:
            side = "SELL"
        elif deviation < -self.band_pct:
            side = "BUY"
        else:
            return None

        return Order(
            agent_id=self.agent_id,
            side=side,
            order_type="MARKET",
            price=None,
            volume=self.volume,
            tick=tick,
        )
