"""
Mean-reversion agent.

Bets against short-term moves: when the latest price deviates from the recent
average by more than a threshold, it takes a position in the opposite
direction, expecting price to revert.
"""
from __future__ import annotations

from typing import Optional

from simulation.agents.base_agent import BaseAgent
from simulation.engine.order import Order


class MeanReversion(BaseAgent):
    """Counter-trend trader that fades extended deviations from the mean."""

    def __init__(self, agent_id: str, cfg: dict, rng=None) -> None:
        super().__init__(agent_id, cfg, rng)
        self.lookback: int = int(cfg.get("mr_lookback", 10))
        self.volume: float = float(cfg.get("mr_volume", 1.0))
        self.dev_threshold: float = float(cfg.get("mr_dev_threshold", 0.001))

    def act(self, context: dict, tick: int) -> Optional[Order]:
        closes = context.get("recent_closes") or []
        if len(closes) < self.lookback + 1:
            return None

        avg = sum(closes[-self.lookback:]) / self.lookback
        last = closes[-1]
        deviation = (last - avg) / (avg + 1e-12)

        # Fade the move: if price extended above mean -> sell, below -> buy.
        if deviation > self.dev_threshold:
            side = "SELL"
        elif deviation < -self.dev_threshold:
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
