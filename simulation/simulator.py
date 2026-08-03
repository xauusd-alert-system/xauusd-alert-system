"""
MarketSimulator: the top-level orchestrator for the virtual XAUUSD market.

It wires together all simulation components:

- :class:`simulation.environment.clock.SimClock`     - discrete-event clock
- :class:`simulation.environment.news_injector.NewsInjector` - Poisson news shocks
- :class:`simulation.engine.order_book.OrderBook`    - price-time priority LOB
- :class:`simulation.engine.matching_engine.MatchingEngine` - order matching
- :class:`simulation.engine.ohlcv_aggregator.OHLCVAggregator`  - tick->OHLCV bars
- :class:`simulation.agents.*`                       - simulated participants

The simulator exposes the narrow API consumed by the MT5 shim and the
simulation entry point: ``step()``, ``warm_up(n_ticks)`` and the
``current_mid_price`` / ``current_ask`` / ``current_bid`` properties, plus
``advance_to_next_m5_bar()`` which returns a just-closed M5 bar as a plain
dict with keys ``time`` / ``open`` / ``high`` / ``low`` / ``close`` /
``volume`` (Unix-seconds timestamps) compatible with the numpy structured
array produced by ``copy_rates_from_pos`` in the MT5 shim.
"""

from __future__ import annotations

import logging
import os
import random
from typing import Optional

import yaml

from simulation.agents.fundamental_agent import FundamentalAgent
from simulation.agents.market_maker import MarketMaker
from simulation.agents.mean_reversion import MeanReversion
from simulation.agents.noise_trader import NoiseTrader
from simulation.agents.trend_follower import TrendFollower
from simulation.engine.matching_engine import MatchingEngine
from simulation.engine.ohlcv_aggregator import Bar, OHLCVAggregator
from simulation.engine.order import Order
from simulation.engine.order_book import OrderBook
from simulation.environment.clock import SimClock
from simulation.environment.news_injector import NewsInjector

logger = logging.getLogger(__name__)

_DEFAULT_CFG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "config",
    "simulation_config.yaml",
)


def load_simulation_config(path: Optional[str] = None) -> dict:
    """Load the simulation configuration YAML into a plain dict."""
    path = path or _DEFAULT_CFG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"Simulation config {path} must be a mapping")
    return cfg


def shutdown_mt5_shim() -> None:
    """Release the injected MT5 shim (called by run_simulation on exit)."""
    try:
        # Lazy import: the shim package is created under simulation/mt5_shim
        # and injected onto sys.path by scripts/run_simulation.py.
        from simulation.mt5_shim import MetaTrader5 as _mt5  # type: ignore

        _mt5.shutdown()
        logger.info("MT5 shim shut down.")
    except Exception:  # pragma: no cover - defensive on shutdown
        logger.debug("MT5 shim shutdown failed or not injected", exc_info=True)


class MarketSimulator:
    """Drives the discrete-event LOB simulation tick by tick."""

    def __init__(
        self,
        cfg: Optional[dict] = None,
        seed: Optional[int] = None,
    ) -> None:
        self.cfg: dict = cfg if cfg is not None else load_simulation_config()
        self.seed = seed
        self.rng = random.Random(seed)

        # --- time --------------------------------------------------------
        self.clock = SimClock(0)
        self.tick: int = 0

        # --- market microstructure ---------------------------------------
        self.book = OrderBook()
        self.engine = MatchingEngine(self.book)
        self.aggregator = OHLCVAggregator(
            bar_interval_ticks=int(self.cfg.get("bar_interval_ticks", 12)),
            tick_duration_seconds=int(self.cfg.get("tick_duration_seconds", 5)),
            start_tick=int(self.cfg.get("bar_interval_ticks", 12)),
        )
        self.engine.on_trade = self._on_trade

        # --- news --------------------------------------------------------
        self.news = NewsInjector(self.cfg, rng=self.rng, clock=self.clock)

        # --- account / symbol --------------------------------------------
        self.initial_price: float = float(self.cfg.get("initial_price", 2400.0))
        self.spread_ask_offset: float = float(
            self.cfg.get("spread_ask_offset", 0.30)
        )
        self.spread_bid_offset: float = float(
            self.cfg.get("spread_bid_offset", 0.30)
        )
        self.last_price: float = self.initial_price
        self.m5_bar_interval_ticks: int = int(
            self.cfg.get("m5_bar_interval_ticks", 60)
        )
        self.last_m5_bar_tick: int = 0

        # --- circuit breaker ---------------------------------------------
        self.circuit_breaker_pct: float = float(
            self.cfg.get("circuit_breaker_pct", 0.15)
        )
        self.start_equity: float = float(self.cfg.get("virtual_balance", 10000.0))
        self.paused: bool = False

        # --- agents ------------------------------------------------------
        self._build_agents()

        # --- housekeeping ------------------------------------------------
        self.quote_refresh_interval: int = int(
            self.cfg.get("quote_refresh_interval", 12)
        )
        self._quote_lifetime: int = int(
            self.cfg.get("quote_lifetime_ticks", 60)
        )
        self.max_ticks_per_step: int = int(
            self.cfg.get("max_ticks_per_step", 1_000_000)
        )
        self.price_history: list[float] = [self.initial_price]

        self._seed_initial_quotes()

    # ------------------------------------------------------------------
    # Agent construction
    # ------------------------------------------------------------------
    def _build_agents(self) -> None:
        cfg = self.cfg
        self.agents: list = []
        n_noise = int(cfg.get("num_noise_traders", 300))
        n_mm = int(cfg.get("num_market_makers", 20))
        n_trend = int(cfg.get("num_trend_followers", 50))
        n_mr = int(cfg.get("num_mean_reversion", 50))
        n_fund = int(cfg.get("num_fundamental", 5))

        for i in range(n_noise):
            self.agents.append(
                NoiseTrader(f"noise-{i}", cfg, rng=random.Random(self.rng.random()))
            )
        for i in range(n_mm):
            self.agents.append(
                MarketMaker(f"mm-{i}", cfg, rng=random.Random(self.rng.random()))
            )
        for i in range(n_trend):
            self.agents.append(
                TrendFollower(f"trend-{i}", cfg, rng=random.Random(self.rng.random()))
            )
        for i in range(n_mr):
            self.agents.append(
                MeanReversion(f"mr-{i}", cfg, rng=random.Random(self.rng.random()))
            )
        for i in range(n_fund):
            self.agents.append(
                FundamentalAgent(f"fund-{i}", cfg, rng=random.Random(self.rng.random()))
            )

    # ------------------------------------------------------------------
    # Initial liquidity
    # ------------------------------------------------------------------
    def _seed_initial_quotes(self) -> None:
        """Place a sparse initial two-sided book around the starting price."""
        mid = self.initial_price
        for level in range(1, 6):
            spread = level * max(self.spread_ask_offset, 0.05)
            self.book.add_limit_order(
                Order(
                    agent_id="seed",
                    side="BUY",
                    order_type="LIMIT",
                    price=round(mid - spread, 6),
                    volume=1.0,
                    tick=0,
                )
            )
            self.book.add_limit_order(
                Order(
                    agent_id="seed",
                    side="SELL",
                    order_type="LIMIT",
                    price=round(mid + spread, 6),
                    volume=1.0,
                    tick=0,
                )
            )

    # ------------------------------------------------------------------
    # Public market API (consumed by the shim / entry point)
    # ------------------------------------------------------------------
    @property
    def current_mid_price(self) -> float:
        mid = self.book.mid_price()
        if mid is None:
            return self.last_price
        return mid

    @property
    def current_bid(self) -> float:
        return self.current_mid_price - self.spread_bid_offset

    @property
    def current_ask(self) -> float:
        return self.current_mid_price + self.spread_ask_offset

    def _on_trade(self, trade) -> None:
        """Feed matched trades into the OHLCV aggregator."""
        self.aggregator.on_tick(trade)

    # ------------------------------------------------------------------
    # Stepping
    # ------------------------------------------------------------------
    def step(self, n_ticks: int = 1) -> int:
        """Advance the simulation by ``n_ticks`` ticks and process events."""
        n_ticks = max(1, int(n_ticks))
        for _ in range(n_ticks):
            if self.tick >= self.max_ticks_per_step:
                break
            self._step_one()
        return self.tick

    def _step_one(self) -> None:
        # 1) Advance the clock so scheduled events (news checks, etc.) fire.
        self.clock.advance(1)
        self.tick = self.clock.tick

        # 2) Periodically purge stale resting quotes so the book stays fresh.
        if self.tick % self.quote_refresh_interval == 0:
            self._refresh_stale_quotes()

        # 3) Circuit breaker check.
        self._update_circuit_breaker()

        # 4) Let agents act and match.
        if not self.paused:
            orders = self._gather_orders()
            self.engine.process_batch(orders)

        # 5) Track the last closed M5 boundary.
        if self.tick % self.m5_bar_interval_ticks == 0:
            self.last_m5_bar_tick = self.tick

        # 6) Keep a short price trail for statistics and mid fallback.
        self.price_history.append(self.current_mid_price)
        if len(self.price_history) > 10_000:
            self.price_history = self.price_history[-5_000:]

    def _gather_orders(self) -> list[Order]:
        mid = self.current_mid_price
        bid = self.current_bid
        ask = self.current_ask

        closes: list[float] = []
        try:
            df = self.aggregator.get_bars_by_interval(
                int(self.cfg.get("bar_interval_ticks", 12)), n=30
            )
            closes = [float(x) for x in df["close"].tolist()]
        except Exception:  # pragma: no cover - defensive
            logger.debug("aggregator bars unavailable", exc_info=True)
        if closes:
            closes = closes + [self.last_price]
        else:
            closes = [self.last_price]

        context = {
            "mid": mid,
            "best_bid": bid,
            "best_ask": ask,
            "last_price": self.last_price,
            "last_bar": None,
            "recent_closes": closes,
            "inventory": 0.0,
            "news_shock": self.news.news_shock,
        }

        orders: list[Order] = []
        for agent in self.agents:
            if not agent.active:
                continue
            order = agent.act(context, self.tick)
            if order is not None:
                orders.append(order)

        # Update last_price from the most recent execution (if any).
        for order in orders:
            if order.side in ("BUY", "SELL") and order.price is not None:
                pass
        return orders

    def _refresh_stale_quotes(self) -> None:
        """Cancel resting limit orders older than the configured lifetime."""
        stale_by = self._quote_lifetime
        for side in (self.book.bids, self.book.asks):
            for _price, _seq, order in list(side):
                if order.tick <= self.tick - stale_by:
                    self.book.cancel_order(order.order_id)

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------
    def _update_circuit_breaker(self) -> None:
        drawdown = (self.current_mid_price - self.initial_price) / self.initial_price
        if abs(drawdown) >= self.circuit_breaker_pct:
            self.paused = True

    # ------------------------------------------------------------------
    # Warm-up / bar helpers
    # ------------------------------------------------------------------
    def warm_up(self, n_ticks: int = 5000) -> None:
        """Run the simulation for ``n_ticks`` to build bar history."""
        n_ticks = max(0, int(n_ticks))
        logger.info("Warming up simulation for %d ticks ...", n_ticks)
        self.step(n_ticks)
        logger.info(
            "Warm-up complete: tick=%d mid=%.2f m5_bar_tick=%d",
            self.tick,
            self.current_mid_price,
            self.last_m5_bar_tick,
        )

    def advance_to_next_m5_bar(self, max_ticks: int = 240) -> Optional[dict]:
        """
        Advance the simulation until the next M5 bar boundary closes and
        return that closed bar as ``{time, open, high, low, close, volume}``
        (``time`` is Unix-seconds). Returns ``None`` if the boundary could
        not be reached within ``max_ticks``.
        """
        target = self.last_m5_bar_tick + self.m5_bar_interval_ticks
        advanced = 0
        while self.tick < target and advanced < max_ticks:
            self.step()
            advanced += 1
            if self.last_m5_bar_tick >= target:
                break

        if self.last_m5_bar_tick < target:
            return None

        try:
            df = self.aggregator.get_bars_by_interval(
                self.m5_bar_interval_ticks, n=2
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("no M5 bars yet", exc_info=True)
            return None
        if df is None or len(df) == 0:
            return None

        # The last row is the bar that just closed at the boundary.
        row = df.iloc[-1]
        return {
            "time": int(row["timestamp"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }

    def last_closed_m5_bars(self, n: int = 10) -> list[Bar]:
        """Return the last ``n`` closed M5 bars (excluding the forming one)."""
        df = self.aggregator.get_bars_by_interval(self.m5_bar_interval_ticks, n=n + 1)
        rows: list[Bar] = []
        if df is not None and len(df) > 0:
            for _, r in df.iterrows():
                rows.append(
                    {
                        "time": int(r["timestamp"]),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["volume"]),
                    }
                )
        return rows

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"MarketSimulator(tick={self.tick}, mid={self.current_mid_price:.2f}, "
            f"agents={len(self.agents)}, book_depth={len(self.book)}, "
            f"news_shock={self.news.news_shock:.5f})"
        )
