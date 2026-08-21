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
        from simulation.mt5_shim import MetaTrader5 as _mt5  # type: ignore
        _mt5.shutdown()
        logger.info("MT5 shim shut down.")
    except Exception:
        logger.debug("MT5 shim shutdown failed or not injected", exc_info=True)


class MarketSimulator:
    """Drives the discrete-event LOB simulation tick by tick."""

    _virtual_state = None

    # Flag: True while warm_up() is running. Circuit breaker is
    # suppressed during warm-up so agents can build bar history freely.
    _warming_up: bool = False

    def __init__(
        self,
        cfg: Optional[dict] = None,
        seed: Optional[int] = None,
    ) -> None:
        self.cfg: dict = cfg if cfg is not None else load_simulation_config()
        self.seed = seed
        self.rng = random.Random(seed)

        self._start_ts: int = int(self.cfg.get("start_timestamp_utc", 1700000000))
        self.clock = SimClock(0)
        self.tick: int = 0

        self.book = OrderBook()
        self.engine = MatchingEngine(self.book)
        self.aggregator = OHLCVAggregator(
            bar_interval_ticks=int(self.cfg.get("bar_interval_ticks", 12)),
            tick_duration_seconds=int(self.cfg.get("tick_duration_seconds", 5)),
            start_tick=0,
            start_timestamp=self._start_ts,
        )
        self.engine.on_trade = self._on_trade

        self.news = NewsInjector(self.cfg, rng=self.rng, clock=self.clock)

        self.initial_price: float = float(self.cfg.get("initial_price", 2400.0))
        self.spread_ask_offset: float = float(self.cfg.get("spread_ask_offset", 0.30))
        self.spread_bid_offset: float = float(self.cfg.get("spread_bid_offset", 0.30))
        self.last_price: float = self.initial_price
        self.m5_bar_interval_ticks: int = int(self.cfg.get("m5_bar_interval_ticks", 60))
        self.last_m5_bar_tick: int = 0

        self.circuit_breaker_pct: float = float(self.cfg.get("circuit_breaker_pct", 0.05))
        self.start_equity: float = float(self.cfg.get("virtual_balance", 10000.0))
        self.paused: bool = False

        # Rolling anchor for circuit breaker; reset after warm_up.
        self._cb_anchor: float = self.initial_price

        self._build_agents()

        self.quote_refresh_interval: int = int(self.cfg.get("quote_refresh_interval", 12))
        self._quote_lifetime: int = int(self.cfg.get("quote_lifetime_ticks", 60))
        self.max_ticks_per_step: int = int(self.cfg.get("max_ticks_per_step", 1_000_000))
        self.price_history: list[float] = [self.initial_price]

        self._seed_book_quotes(self.initial_price)

    def attach_state(self, state) -> None:
        self._virtual_state = state

    def _build_agents(self) -> None:
        cfg = self.cfg
        self.agents: list = []
        for i in range(int(cfg.get("num_noise_traders", 200))):
            self.agents.append(NoiseTrader(f"noise-{i}", cfg, rng=random.Random(self.rng.random())))
        for i in range(int(cfg.get("num_market_makers", 20))):
            self.agents.append(MarketMaker(f"mm-{i}", cfg, rng=random.Random(self.rng.random())))
        for i in range(int(cfg.get("num_trend_followers", 150))):
            self.agents.append(TrendFollower(f"trend-{i}", cfg, rng=random.Random(self.rng.random())))
        for i in range(int(cfg.get("num_mean_reversion", 20))):
            self.agents.append(MeanReversion(f"mr-{i}", cfg, rng=random.Random(self.rng.random())))
        for i in range(int(cfg.get("num_fundamental", 20))):
            self.agents.append(FundamentalAgent(f"fund-{i}", cfg, rng=random.Random(self.rng.random())))

    def _seed_book_quotes(self, mid: float) -> None:
        """Plant thin resting limit orders around `mid` to guarantee a spread."""
        for level in range(1, 6):
            spread = level * max(self.spread_ask_offset, 0.05)
            self.book.add_limit_order(Order(
                agent_id="seed", side="BUY", order_type="LIMIT",
                price=round(mid - spread, 6), volume=1.0, tick=self.tick,
            ))
            self.book.add_limit_order(Order(
                agent_id="seed", side="SELL", order_type="LIMIT",
                price=round(mid + spread, 6), volume=1.0, tick=self.tick,
            ))

    @property
    def current_mid_price(self) -> float:
        mid = self.book.mid_price()
        return mid if mid is not None else self.last_price

    @property
    def current_bid(self) -> float:
        return self.current_mid_price - self.spread_bid_offset

    @property
    def current_ask(self) -> float:
        return self.current_mid_price + self.spread_ask_offset

    def _on_trade(self, trade) -> None:
        self.aggregator.on_tick(trade)
        self.last_price = trade.price

    def step(self, n_ticks: int = 1) -> int:
        n_ticks = max(1, int(n_ticks))
        for _ in range(n_ticks):
            if self.tick >= self.max_ticks_per_step:
                break
            self._step_one()
        return self.tick

    def _step_one(self) -> None:
        self.clock.advance(1)
        self.tick = self.clock.tick

        if self.tick % self.quote_refresh_interval == 0:
            self._refresh_stale_quotes()

        # Check circuit breaker only outside warm-up.
        if not self._warming_up:
            self._update_circuit_breaker()

        if not self.paused:
            orders = self._gather_orders()
            self.engine.process_batch(orders)

        if self.tick % self.m5_bar_interval_ticks == 0:
            self.last_m5_bar_tick = self.tick
            # Rolling CB anchor: re-anchor on each closed M5 bar so a slow
            # multi-bar drift never trips the breaker permanently. The ±pct
            # band still halts single-bar shocks (checked above against the
            # previous bar's anchor).
            if not self._warming_up and not self.paused:
                self._cb_anchor = self.current_mid_price

        self.price_history.append(self.current_mid_price)
        if len(self.price_history) > 10_000:
            self.price_history = self.price_history[-5_000:]

        if self._virtual_state is not None:
            mid = self.current_mid_price
            for pos in self._virtual_state.positions.values():
                pos.price_current = round(mid, 6)

    def _gather_orders(self) -> list[Order]:
        mid = self.current_mid_price
        closes: list[float] = []
        try:
            df = self.aggregator.get_bars_by_interval(
                int(self.cfg.get("bar_interval_ticks", 12)), n=30
            )
            closes = [float(x) for x in df["close"].tolist()]
        except Exception:
            logger.debug("aggregator bars unavailable", exc_info=True)
        closes = (closes + [self.last_price]) if closes else [self.last_price]

        net_inventory: float = 0.0
        if self._virtual_state is not None:
            for pos in self._virtual_state.positions.values():
                net_inventory += pos.volume if pos.type == 0 else -pos.volume

        context = {
            "mid": mid,
            "best_bid": self.current_bid,
            "best_ask": self.current_ask,
            "last_price": self.last_price,
            "last_bar": None,
            "recent_closes": closes,
            "inventory": net_inventory,
            "news_shock": self.news.news_shock,
        }

        orders: list[Order] = []
        for agent in self.agents:
            if agent.active:
                order = agent.act(context, self.tick)
                if order is not None:
                    orders.append(order)
        return orders

    def _refresh_stale_quotes(self) -> None:
        stale_by = self._quote_lifetime
        for side in (self.book.bids, self.book.asks):
            for _price, _seq, order in list(side):
                if order.tick <= self.tick - stale_by:
                    self.book.cancel_order(order.order_id)

    def _update_circuit_breaker(self) -> None:
        """Trip (but never auto-reset) the breaker when price is >circuit_breaker_pct
        from the rolling anchor. Recovery is explicit via warm_up() only.
        """
        if self.paused:
            return  # already tripped – don't spam warnings
        mid = self.current_mid_price
        deviation = abs(mid - self._cb_anchor) / (self._cb_anchor + 1e-12)
        if deviation >= self.circuit_breaker_pct:
            logger.warning(
                "Circuit breaker tripped: mid=%.2f anchor=%.2f dev=%.3f",
                mid, self._cb_anchor, deviation,
            )
            self.paused = True

    def warm_up(self, n_ticks: int = 5000) -> None:
        """Run n_ticks of simulation to build bar history.

        Post-warm-up resets:
        1. Re-anchors the CB to the current mid so live-run starts clean.
        2. Resets the aggregator with the current tick as new start_tick so
           live bars carry correct incrementing timestamps.
        3. Resets last_m5_bar_tick to self.tick so advance_to_next_m5_bar
           doesn't try to catch up to a stale target from the warm-up.
        4. Re-seeds the order book around current mid (not initial_price)
           so no immediate CB trip from stale seed quotes.
        5. Clears paused flag.
        """
        n_ticks = max(0, int(n_ticks))
        logger.info("Warming up simulation for %d ticks ...", n_ticks)
        self._warming_up = True
        self.step(n_ticks)
        self._warming_up = False

        current_mid = self.current_mid_price
        logger.info(
            "Warm-up complete: tick=%d mid=%.2f m5_bar_tick=%d",
            self.tick, current_mid, self.last_m5_bar_tick,
        )

        # 1. Re-anchor CB to post-warmup price.
        self._cb_anchor = current_mid
        # 2. Reset aggregator – pass current tick so new bars index from here.
        self.aggregator.reset(new_start_tick=self.tick)
        # 3. Align last_m5_bar_tick so advance_to_next_m5_bar advances correctly.
        self.last_m5_bar_tick = self.tick
        # 4. Re-seed order book around the current (post-warmup) mid price.
        self._seed_book_quotes(current_mid)
        # 5. Clear any tripped state.
        self.paused = False

    def advance_to_next_m5_bar(self, max_ticks: int = 240) -> Optional[dict]:
        """Advance simulation until the next M5 bar closes, return it.

        Returns None if the bar didn't close within max_ticks (caller
        will retry on the next driver iteration).
        """
        target = self.last_m5_bar_tick + self.m5_bar_interval_ticks
        advanced = 0
        while self.tick < target and advanced < max_ticks:
            self.step()
            advanced += 1

        if self.last_m5_bar_tick < target:
            return None  # didn't reach boundary yet

        try:
            df = self.aggregator.get_bars_by_interval(self.m5_bar_interval_ticks, n=2)
        except Exception:
            logger.debug("no M5 bars yet", exc_info=True)
            return None
        if df is None or len(df) == 0:
            return None

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
        df = self.aggregator.get_bars_by_interval(self.m5_bar_interval_ticks, n=n + 1)
        rows: list[Bar] = []
        if df is not None and len(df) > 0:
            for _, r in df.iterrows():
                rows.append({
                    "time": int(r["timestamp"]),
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]),
                })
        return rows

    def __repr__(self) -> str:
        return (
            f"MarketSimulator(tick={self.tick}, mid={self.current_mid_price:.2f}, "
            f"agents={len(self.agents)}, book_depth={len(self.book)}, "
            f"news_shock={self.news.news_shock:.5f})"
        )
