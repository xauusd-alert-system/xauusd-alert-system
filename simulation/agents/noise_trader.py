"""
Uninformed liquidity-taker agent.

Noise traders arrive according to a Poisson process (rate = noise_lambda per
tick). When active they submit a random-size market order in a random
direction, based on a lognormal order-size distribution clamped to
[noise_min_size, noise_max_size].
"""
from __future__ import annotations

import math
from typing import Optional

from simulation.agents.base_agent import BaseAgent
from simulation.engine.order import Order


class NoiseTrader(BaseAgent):
    """Random liquidity taker that generates the market's order flow."""

    def __init__(self, agent_id: str, cfg: dict, rng=None) -> None:
        super().__init__(agent_id, cfg, rng)
        self.lambda_rate: float = float(cfg.get("noise_lambda", 0.3))
        self.log_mu: float = float(cfg.get("noise_lognormal_mu", 1.5))
        self.log_sigma: float = float(cfg.get("noise_lognormal_sigma", 0.5))
        self.min_size: float = float(cfg.get("noise_min_size", 0.01))
        self.max_size: float = float(cfg.get("noise_max_size", 5.0))
        # Directional "tide": the BUY probability oscillates as a sine wave in
        # time (default sweep 0.45..0.55) so local buy/sell pressure arrives in
        # waves instead of a perfectly symmetric 50/50 flip that pins the price.
        self.buy_prob_amplitude: float = float(
            cfg.get("noise_imbalance_amplitude", 0.05)
        )
        self.buy_prob_period: float = float(
            cfg.get("noise_imbalance_period_ticks", 480.0)
        )
        self._phase: float = self.rng.uniform(0.0, 2.0 * math.pi)

    def _buy_probability(self, tick: int) -> float:
        """Time-varying BUY probability: 0.5 +/- sine wave (default 45%..55%)."""
        wave = self.buy_prob_amplitude * math.sin(
            2.0 * math.pi * tick / self.buy_prob_period + self._phase
        )
        return 0.5 + wave

    def _draw_size(self) -> float:
        """Sample a lognormal order size and clamp to the configured range."""
        # random.Random exposes gauss(), not the numpy Generator's normal().
        raw = math.exp(self.rng.gauss(self.log_mu, self.log_sigma))
        return round(max(self.min_size, min(self.max_size, raw)), 2)

    def act(self, context: dict, tick: int) -> Optional[Order]:
        # Poisson arrival: prob of >=1 event in a tick of the given rate.
        if self.rng.random() >= self.lambda_rate:
            return None

        side = "BUY" if self.rng.random() < self._buy_probability(tick) else "SELL"
        volume = self._draw_size()

        return Order(
            agent_id=self.agent_id,
            side=side,
            order_type="MARKET",
            price=None,
            volume=volume,
            tick=tick,
        )
