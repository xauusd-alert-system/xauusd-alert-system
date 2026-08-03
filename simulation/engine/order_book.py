"""
Limit order book for the virtual market simulation.

Uses sortedcontainers.SortedList for O(log n) ordered price levels on both
the bid (BUY) and ask (SELL) side. Price-time priority is enforced by
ordering entries by (price, sequence).
"""
from __future__ import annotations

from sortedcontainers import SortedList

from simulation.engine.order import Order


class OrderBook:
    """Central price-time priority order book with SortedList bids/asks."""

    def __init__(self) -> None:
        # Entries stored as (price, seq, order) tuples.
        # Bids: descending price priority -> store negative price for sortability.
        # Asks: ascending price priority -> store price directly.
        self.bids: SortedList = SortedList()   #  ( -price, seq, order )
        self.asks: SortedList = SortedList()   #  (  price, seq, order )
        self._seq: int = 0
        self._orders: dict[str, "tuple"] = {}  # order_id -> book entry tuple
        self._price_bids: dict[float, float] = {}
        self._price_asks: dict[float, float] = {}

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------
    def add_limit_order(self, order: Order) -> None:
        """Insert a resting limit order using price-time priority."""
        if order.order_type != "LIMIT" or order.price is None:
            raise ValueError(f"Expected LIMIT order with a price, got {order}")

        seq = self._seq
        self._seq += 1

        if order.is_buy:
            entry = (-order.price, seq, order)
            self.bids.add(entry)
            self._price_bids[order.price] = self._price_bids.get(order.price, 0.0) + order.volume
        else:
            entry = (order.price, seq, order)
            self.asks.add(entry)
            self._price_asks[order.price] = self._price_asks.get(order.price, 0.0) + order.volume

        self._orders[order.order_id] = entry

    def cancel_order(self, order_id: str) -> bool:
        """Remove a resting order by id. Returns True if cancelled."""
        entry = self._orders.pop(order_id, None)
        if entry is None:
            return False

        price, seq, order = entry
        if order.is_buy:
            self.bids.discard(entry)
            new_vol = self._price_bids.get(price, 0.0) - order.volume
            if new_vol <= 1e-12:
                self._price_bids.pop(price, None)
            else:
                self._price_bids[price] = new_vol
        else:
            self.asks.discard(entry)
            new_vol = self._price_asks.get(price, 0.0) - order.volume
            if new_vol <= 1e-12:
                self._price_asks.pop(price, None)
            else:
                self._price_asks[price] = new_vol
        return True

    def reduce_volume(self, order_id: str, reduce_by: float) -> None:
        """Reduce remaining volume of a partially filled resting order."""
        entry = self._orders.get(order_id)
        if entry is None:
            return
        price, seq, order = entry
        order.volume -= reduce_by
        if order.volume <= 1e-12:
            self.cancel_order(order_id)

    # ------------------------------------------------------------------
    # Best quotes
    # ------------------------------------------------------------------
    def best_bid(self) -> float | None:
        """Highest outstanding buy limit price."""
        if not self.bids:
            return None
        return -self.bids[0][0]

    def best_ask(self) -> float | None:
        """Lowest outstanding sell limit price."""
        if not self.asks:
            return None
        return self.asks[0][0]

    def mid_price(self) -> float | None:
        """Midpoint of the current inside market."""
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    def spread(self) -> float | None:
        """Inside spread: ask - bid. None if either side is empty."""
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def volume_at_price(self, side: str, price: float) -> float:
        """Total resting volume at a specific price level."""
        if side == "BUY":
            return self._price_bids.get(price, 0.0)
        return self._price_asks.get(price, 0.0)

    # ------------------------------------------------------------------
    # Depth snapshots
    # ------------------------------------------------------------------
    def depth_snapshot(self, levels: int = 10) -> dict:
        """Return a compact dict describing top-of-book depth."""
        bids = []
        for price, seq, order in self.bids[:levels]:
            bid_price = -price
            if bids and abs(bids[-1][0] - bid_price) < 1e-12:
                bids[-1] = (bid_price, bids[-1][1] + order.volume)
            else:
                bids.append((bid_price, order.volume))

        asks = []
        for price, seq, order in self.asks[:levels]:
            if asks and abs(asks[-1][0] - price) < 1e-12:
                asks[-1] = (price, asks[-1][1] + order.volume)
            else:
                asks.append((price, order.volume))

        return {
            "bids": bids,
            "asks": asks,
            "best_bid": self.best_bid(),
            "best_ask": self.best_ask(),
            "mid": self.mid_price(),
            "spread": self.spread(),
        }

    def __len__(self) -> int:
        return len(self._orders)
