"""
Fundamental / rebalancing agent.

Maintains a slow-moving 'fundamental' anchor that itself performs a random
walk (drift) each tick.  The agent trades toward the anchor, so as the anchor
drifts the whole market gets pulled in that direction, generating sustained
directional moves instead of perpetual mean-reversion.

News shocks SHIFT the anchor (not just the deviation calculation), producing
lasting directional pressure rather than a one-tick blip.
"""
from __future__ import annotations

from typing import Optional

from simulation.agents.base_agent import BaseAgent
from simulation.engine.order import Order


class FundamentalAgent(BaseAgent):
    """Rebalancer whose fundamental anchor itself drifts, generating trends."""

    def __init__(self, agent_id: str, cfg: dict, rng=None) -> None:
        super().__init__(agent_id, cfg, rng)
        self.impact_factor: float = float(cfg.get("fundamental_impact_factor", 2.0))
        self.anchor: float = float(cfg.get("initial_price", 2400.0))
        self.volume: float = float(cfg.get("fundamental_volume", 1.0))
        self.band_pct: float = float(cfg.get("fundamental_band_pct", 0.0002))
        # Random-walk drift: each tick the anchor moves by ±drift_pct * price
        # This is the key parameter that makes the price *go somewhere*.
        self.drift_pct: float = float(cfg.get("fundamental_drift_pct", 0.0003))
        self._last_shock: float = 0.0

    def act(self, context: dict, tick: int) -> Optional[Order]:
        mid = context.get("mid")
        if mid is None:
            return None

        # 1) Drift the anchor by a small random-walk step each tick.
        #    This is what creates a sustained directional move in the market.
        step = (self.rng.random() * 2 - 1) * self.drift_pct * self.anchor
        self.anchor += step

        # 2) News shocks permanently shift the anchor (not just the order side).
        #    A positive shock means 'fair value just jumped up' — anchor follows.
        shock = context.get("news_shock", 0.0) or 0.0
        if abs(shock) > 1e-6:
            self.anchor += shock * self.impact_factor * self.anchor

        # 3) Deviation of market price from the (now-drifting) anchor.
        deviation = (mid - self.anchor) / (self.anchor + 1e-12)

        # 4) Only trade when outside the deadband, pulling price toward anchor.
        if deviation > self.band_pct:
            side = "SELL"
        elif deviation < -self.band_pct:
            side = "BUY"
        else:
            return None

        # Larger dislocations -> larger orders (up to 5x base volume).
        vol = min(self.volume * (1.0 + abs(deviation) * 10.0), self.volume * 5.0)

        return Order(
            agent_id=self.agent_id,
            side=side,
            order_type="MARKET",
            price=None,
            volume=round(vol, 2),
            tick=tick,
        )
