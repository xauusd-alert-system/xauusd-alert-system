"""
SimulationProvider: singleton facade over the MarketSimulator + VirtualState.

Provides a simple candle accessor (``get_candles``) consistent with the
shape used by the ingestion layer, plus read-only access to the live
simulator and virtual account state so the rest of the codebase never has
to reach into the shim internals.

The provider is a process-wide singleton: ``SimulationProvider.get()``
returns the same instance so the MT5 shim, the entry point and any tests
share one simulated world.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from simulation.simulator import MarketSimulator, load_simulation_config
from simulation.virtual_state import VirtualState

logger = logging.getLogger(__name__)

# Timeframe string -> tick interval used by the aggregator.
# M5 = 60 ticks (5s each) = 5 minutes, matching TIMEFRAME_M5.
_TIMEFRAME_INTERVALS = {
    "M1": 12,
    "M5": 60,
    "M15": 180,
    "H1": 720,
}


class SimulationProvider:
    """Singleton façade giving access to the running simulation."""

    _instance: "Optional[SimulationProvider]" = None

    def __init__(
        self,
        cfg: Optional[dict] = None,
        seed: Optional[int] = None,
        warm_up_ticks: int = 5000,
    ) -> None:
        self.cfg: dict = (
            cfg if cfg is not None else load_simulation_config()
        )
        self.simulator = MarketSimulator(cfg=self.cfg, seed=seed)
        self.state = VirtualState(self.cfg)
        self.warm_up_ticks: int = int(warm_up_ticks)

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------
    @classmethod
    def get(cls) -> "SimulationProvider":
        """Return the process-wide singleton, creating it if needed."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Drop the singleton (used by tests)."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def initialize(self, warm_up_ticks: Optional[int] = None) -> "SimulationProvider":
        """Warm up the market and populate bar history.

        Returns self for chaining:
        ``provider = SimulationProvider.get().initialize()``
        """
        if warm_up_ticks is None:
            warm_up_ticks = self.warm_up_ticks
        self.simulator.warm_up(int(warm_up_ticks))
        return self

    # ------------------------------------------------------------------
    # Market data accessors
    # ------------------------------------------------------------------
    def get_candles(
        self,
        timeframe: str = "M5",
        n: int = 300,
    ) -> pd.DataFrame:
        """Return simulated closed candles for a timeframe string.

        Columns: timestamp (int Unix seconds, UTC), open, high, low,
        close, volume.  Matches the schema produced by the MT5 provider so
        the pipeline's feature builder works unchanged.
        """
        interval = _TIMEFRAME_INTERVALS.get(str(timeframe).upper(), 60)
        try:
            df = self.simulator.aggregator.get_bars_by_interval(interval, n=n)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("get_candles failed for TF=%s: %s", timeframe, exc)
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
        if df is None:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
        return df

    # ------------------------------------------------------------------
    # Account helpers
    # ------------------------------------------------------------------
    def equity(self) -> float:
        return self.state.account_info().equity

    def balance(self) -> float:
        return self.state.balance

    def positions_count(self) -> int:
        return len(self.state.positions)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def summary(self) -> dict:
        """Snapshot used for logs / Telegram notifications."""
        acct = self.state.account_info()
        return {
            "tick": self.simulator.tick,
            "mid": self.simulator.current_mid_price,
            "bid": self.simulator.current_bid,
            "ask": self.simulator.current_ask,
            "balance": acct.balance,
            "equity": acct.equity,
            "profit": acct.profit,
            "open_positions": len(self.state.positions),
            "deals": len(self.state.deals),
            "news_shock": self.simulator.news.news_shock,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SimulationProvider(tick={self.simulator.tick}, "
            f"equity={self.equity():.2f})"
        )
