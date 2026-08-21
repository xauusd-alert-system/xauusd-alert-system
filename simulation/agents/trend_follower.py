"""
Momentum / trend-following agent.

Compares the latest close to a short-term moving average.  When momentum is
strong enough it submits a market order in the trend direction.  Volume scales
with momentum magnitude so strong moves get amplified.

Key changes vs original:
- Default min_trend_strength lowered from 0.0002 → 0.00005 (fires 4× more)
- Default lookback shortened from 10 → 5 bars (reacts to short-term moves)
- Volume scales with |strength| * momentum_volume_multiplier
"""
from __future__ import annotations

from typing import Optional

from simulation.agents.base_agent import BaseAgent
from simulation.engine.order import Order


class TrendFollower(BaseAgent):
    """Trades in the direction of recent momentum with amplitude-scaled volume."""

    def __init__(self, agent_id: str, cfg: dict, rng=None) -> None:
        super().__init__(agent_id, cfg, rng)
        # Shorter lookback = reacts to more recent moves
        self.lookback: int = int(cfg.get("trend_lookback", 5))
        self.volume: float = float(cfg.get("trend_volume", 1.0))
        # Lower threshold = fires much more frequently
        self.min_trend_strength: float = float(cfg.get("trend_min_strength", 0.00005))
        # Scales order size with momentum strength
        self.vol_multiplier: float = float(cfg.get("trend_volume_multiplier", 20.0))

    def act(self, context: dict, tick: int) -> Optional[Order]:
        closes = context.get("recent_closes") or []
        if len(closes) < self.lookback + 1:
            return None

        avg = sum(closes[-self.lookback:]) / self.lookback
        last = closes[-1]
        # Normalized displacement: how far current price is from its SMA.
        strength = (last - avg) / (avg + 1e-12)

        if strength > self.min_trend_strength:
            side = "BUY"
        elif strength < -self.min_trend_strength:
            side = "SELL"
        else:
            return None

        # Volume scales with momentum: stronger trend -> bigger order.
        vol = min(
            self.volume * (1.0 + abs(strength) * self.vol_multiplier),
            self.volume * 10.0,  # cap at 10x base volume
        )

        return Order(
            agent_id=self.agent_id,
            side=side,
            order_type="MARKET",
            price=None,
            volume=round(vol, 2),
            tick=tick,
        )
