"""
Base agent interface for the virtual market simulation.

Every simulated participant subclasses BaseAgent and implements act(),
returning an Order (or None when the agent stays quiet on that tick).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from simulation.engine.order import Order


class BaseAgent(ABC):
    """Common interface shared by all simulated market participants."""

    def __init__(self, agent_id: str, cfg: dict, rng=None) -> None:
        self.agent_id = agent_id
        self.cfg = cfg
        self.rng = rng
        self.active: bool = True

    @abstractmethod
    def act(self, context: dict, tick: int) -> Optional[Order]:
        """
        Decide what this agent does on a given tick.

        context: dict with market info the agent can observe, e.g.
                 {"mid": float, "best_bid": float, "best_ask": float,
                  "last_price": float, "last_bar": dict, "inventory": float,
                  "news_shock": float or None}
        tick:    current simulation tick (int).
        Returns an Order or None.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}(agent_id={self.agent_id!r}, active={self.active})"
