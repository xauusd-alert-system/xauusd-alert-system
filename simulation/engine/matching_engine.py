"""
Matching engine for the virtual limit order book.

Processes incoming market and limit orders against the resting book and
emits Trade objects for each match, applying price-time priority.
"""
from __future__ import annotations

from uuid import uuid4
from typing import Callable

from simulation.engine.order import Order, Trade
from simulation.engine.order_book import OrderBook


class MatchingEngine:
    """Crosses orders against the OrderBook and produces Trades."""

    def __init__(self, book: OrderBook) -> None:
        self.book = book
        self.trades: list[Trade] = []
        self.on_trade: Callable[[Trade], None] | None = None

    def process(self, order: Order) -> list[Trade]:
        """Process a single incoming order; returns the trades it generated."""
        if order.order_type == "MARKET":
            return self._fill_market(order)
        elif order.order_type == "LIMIT":
            return self._fill_limit(order)
        elif order.order_type == "CANCEL":
            self.book.cancel_order(order.order_id)
            return []
        raise ValueError(f"Unknown order_type: {order.order_type}")

    def process_batch(self, orders: list[Order]) -> list[Trade]:
        """Process a batch of orders in arrival order, aggregating trades."""
        all_trades: list[Trade] = []
        for order in orders:
            all_trades.extend(self.process(order))
        return all_trades

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------
    def _fill_market(self, order: Order) -> list[Trade]:
        """Execute a market order at the best available prices."""
        trades: list[Trade] = []
        remaining = order.volume
        counter_side_prefix = "asks" if order.is_buy else "bids"

        while remaining > 1e-12:
            side = getattr(self.book, counter_side_prefix)
            if not side:
                break  # illiquid: leave order unfilled
            price, seq, resting = side[0]
            if not order.is_buy:
                price = -price  # bids entries store (-price, seq, order)
            fill_vol = min(remaining, resting.volume)
            trades.append(self._make_trade(order, resting, price, fill_vol))
            remaining -= fill_vol
            self.book.reduce_volume(resting.order_id, fill_vol)

        return trades

    def _fill_limit(self, order: Order) -> list[Trade]:
        """
        Cross a limit order against the book first; any unfilled remainder
        rests in the book.
        """
        trades: list[Trade] = []
        remaining = order.volume

        if order.price is None:
            return trades

        while remaining > 1e-12:
            if order.is_buy:
                best = self.book.best_ask()
                if best is None or best > order.price:
                    break  # no crossable ask
                price, seq, resting = self.book.asks[0]
            else:
                best = self.book.best_bid()
                if best is None or best < order.price:
                    break  # no crossable bid
                price, seq, resting = self.book.bids[0]
                price = -price

            fill_vol = min(remaining, resting.volume)
            trades.append(self._make_trade(order, resting, price, fill_vol))
            remaining -= fill_vol
            self.book.reduce_volume(resting.order_id, fill_vol)

        if remaining > 1e-12:
            order.volume = remaining
            self.book.add_limit_order(order)

        return trades

    # ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------
    def _make_trade(self, incoming: Order, resting: Order, price: float,
                    volume: float) -> Trade:
        if incoming.is_buy:
            buy_order_id, sell_order_id = incoming.order_id, resting.order_id
        else:
            buy_order_id, sell_order_id = resting.order_id, incoming.order_id

        trade = Trade(
            trade_id=str(uuid4()),
            buy_order_id=buy_order_id,
            sell_order_id=sell_order_id,
            price=round(price, 6),
            volume=round(volume, 6),
            tick=incoming.tick,
        )
        if self.on_trade is not None:
            self.on_trade(trade)
        return trade
