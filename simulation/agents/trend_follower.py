"""
Momentum / trend-following agent.

Compares the latest close to a short-term moving average computed from the
recent bars. When the trend is strong enough it submits a market order in the
trend direction; otherwise it stays quiet.
"""
from __future__ import annotations

from typing import Optional

from simulation.agents.base_agent import BaseAgent
from simulation.engine.order import Order


class TrendFollower(BaseAgent):
    """Trades in the direction of recent momentum."""

    def __init__(self, agent_id: str, cfg: dict, rng=None) -> None:
        super().__init__(agent_id, cfg, rng)
        self.lookback: int = int(cfg.get("trend_lookback", 10))
        self.volume: float = float(cfg.get("trend_volume", 1.0))
        self.min_trend_strength: float = float(cfg.get("trend_min_strength", 0.0002))

    def act(self, context: dict, tick: int) -> Optional[Order]:
        closes = context.get("recent_closes") or []
        if len(closes) < self.lookback + 1:
            return None

        avg = sum(closes[-self.lookback:]) / self.lookback
        last = closes[-1]
        # Normalized displacement: how far the price is from its average.
        strength = (last - avg) / (avg + 1e-12)

        if strength > self.min_trend_strength:
            side = "BUY"
        elif strength < -self.min_trend_strength:
            side = "SELL"
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
