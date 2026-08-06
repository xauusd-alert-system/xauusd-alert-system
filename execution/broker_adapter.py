"""
Phase 9: Multi-Broker Execution Layer & Adapters.
Provides an abstract BrokerAdapter interface and concrete implementations for:
- MT5 (Production Windows terminal)
- Virtual Simulator (Deterministic offline market)
- Mock FIX Protocol (Financial Information eXchange adapter)
- cTrader Open API compatible adapter
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import time
import logging

logger = logging.getLogger("broker_adapter")


@dataclass
class AccountSnapshot:
    balance: float
    equity: float
    margin: float
    free_margin: float
    currency: str = "USD"
    leverage: int = 100


@dataclass
class PositionSnapshot:
    ticket: int
    symbol: str
    direction: str  # "buy" or "sell"
    volume: float
    open_price: float
    current_price: float
    sl: Optional[float]
    tp: Optional[float]
    profit: float
    magic: int = 777111


@dataclass
class OrderResult:
    success: bool
    ticket: Optional[int] = None
    price: Optional[float] = None
    comment: str = ""
    retcode: int = 0


class BaseBrokerAdapter(ABC):
    """Abstract broker interface for execution."""

    @abstractmethod
    def connect(self) -> bool:
        """Initialize connection to the broker."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from broker."""
        pass

    @abstractmethod
    def get_account_info(self) -> AccountSnapshot:
        """Fetch current account balances."""
        pass

    @abstractmethod
    def get_positions(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:
        """Fetch list of active positions."""
        pass

    @abstractmethod
    def open_market_order(
        self,
        symbol: str,
        direction: str,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "",
        magic: int = 777111,
    ) -> OrderResult:
        """Send market execution order."""
        pass

    @abstractmethod
    def close_position(self, ticket: int, volume: Optional[float] = None) -> OrderResult:
        """Close a specific position by ticket."""
        pass

    @abstractmethod
    def modify_position(
        self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None
    ) -> OrderResult:
        """Modify SL/TP of an open position."""
        pass


class MT5BrokerAdapter(BaseBrokerAdapter):
    """Adapter bridging to MetaTrader 5 API."""

    def __init__(self):
        try:
            import MetaTrader5 as mt5
        except ImportError:
            from simulation.mt5_shim import MetaTrader5 as mt5
        self.mt5 = mt5
        self.connected = False

    def connect(self) -> bool:
        self.connected = bool(self.mt5.initialize())
        return self.connected

    def disconnect(self) -> None:
        if self.connected:
            self.mt5.shutdown()
            self.connected = False

    def get_account_info(self) -> AccountSnapshot:
        acc = self.mt5.account_info()
        if acc is None:
            return AccountSnapshot(0.0, 0.0, 0.0, 0.0)
        return AccountSnapshot(
            balance=float(acc.balance),
            equity=float(acc.equity),
            margin=float(getattr(acc, "margin", 0.0)),
            free_margin=float(getattr(acc, "margin_free", 0.0)),
            currency=getattr(acc, "currency", "USD"),
            leverage=int(getattr(acc, "leverage", 100)),
        )

    def get_positions(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:
        raw = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if not raw:
            return []
        res = []
        for p in raw:
            dir_str = "buy" if p.type == 0 else "sell"
            res.append(PositionSnapshot(
                ticket=int(p.ticket),
                symbol=str(p.symbol),
                direction=dir_str,
                volume=float(p.volume),
                open_price=float(p.price_open),
                current_price=float(p.price_current),
                sl=float(p.sl) if p.sl else None,
                tp=float(p.tp) if p.tp else None,
                profit=float(p.profit),
                magic=int(getattr(p, "magic", 0)),
            ))
        return res

    def open_market_order(
        self,
        symbol: str,
        direction: str,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "",
        magic: int = 777111,
    ) -> OrderResult:
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            return OrderResult(success=False, comment=f"No tick data for {symbol}")

        order_type = self.mt5.ORDER_TYPE_BUY if direction.lower() == "buy" else self.mt5.ORDER_TYPE_SELL
        price = tick.ask if direction.lower() == "buy" else tick.bid

        req = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": float(price),
            "deviation": 20,
            "magic": magic,
            "comment": comment,
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        if sl:
            req["sl"] = float(sl)
        if tp:
            req["tp"] = float(tp)

        res = self.mt5.order_send(req)
        if res and res.retcode == self.mt5.TRADE_RETCODE_DONE:
            return OrderResult(success=True, ticket=res.order, price=price, comment="Done", retcode=res.retcode)
        return OrderResult(
            success=False,
            comment=f"MT5 order error: {getattr(res, 'comment', 'unknown')}",
            retcode=getattr(res, "retcode", -1) if res else -1,
        )

    def close_position(self, ticket: int, volume: Optional[float] = None) -> OrderResult:
        positions = self.mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(success=False, comment=f"Position {ticket} not found")
        pos = positions[0]
        tick = self.mt5.symbol_info_tick(pos.symbol)
        if not tick:
            return OrderResult(success=False, comment=f"No tick for {pos.symbol}")

        close_type = self.mt5.ORDER_TYPE_SELL if pos.type == 0 else self.mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == 0 else tick.ask
        vol = float(volume) if volume else float(pos.volume)

        req = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": pos.symbol,
            "volume": vol,
            "type": close_type,
            "price": price,
            "deviation": 20,
            "magic": getattr(pos, "magic", 777111),
            "comment": "Close order",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        res = self.mt5.order_send(req)
        if res and res.retcode == self.mt5.TRADE_RETCODE_DONE:
            return OrderResult(success=True, ticket=res.order, price=price, retcode=res.retcode)
        return OrderResult(success=False, comment=getattr(res, "comment", "failed"), retcode=getattr(res, "retcode", -1) if res else -1)

    def modify_position(
        self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None
    ) -> OrderResult:
        positions = self.mt5.positions_get(ticket=ticket)
        if not positions:
            return OrderResult(success=False, comment=f"Position {ticket} not found")
        pos = positions[0]
        req = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": float(sl) if sl is not None else float(pos.sl or 0.0),
            "tp": float(tp) if tp is not None else float(pos.tp or 0.0),
        }
        res = self.mt5.order_send(req)
        if res and res.retcode == self.mt5.TRADE_RETCODE_DONE:
            return OrderResult(success=True, ticket=ticket, retcode=res.retcode)
        return OrderResult(success=False, comment=getattr(res, "comment", "modify failed"), retcode=getattr(res, "retcode", -1) if res else -1)


class MockFIXBrokerAdapter(BaseBrokerAdapter):
    """
    Mock adapter representing an institutional FIX Protocol 4.4 connection.
    Used for simulation and non-MT5 institutional gateways.
    """

    def __init__(self, initial_balance: float = 100000.0):
        self.balance = initial_balance
        self.equity = initial_balance
        self.positions: Dict[int, PositionSnapshot] = {}
        self.ticket_counter = 10000
        self.connected = False

    def connect(self) -> bool:
        self.connected = True
        return True

    def disconnect(self) -> None:
        self.connected = False

    def get_account_info(self) -> AccountSnapshot:
        unrealized = sum(p.profit for p in self.positions.values())
        self.equity = self.balance + unrealized
        return AccountSnapshot(
            balance=self.balance,
            equity=self.equity,
            margin=len(self.positions) * 1000.0,
            free_margin=self.equity - (len(self.positions) * 1000.0),
            currency="USD",
        )

    def get_positions(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:
        if symbol:
            return [p for p in self.positions.values() if p.symbol == symbol]
        return list(self.positions.values())

    def open_market_order(
        self,
        symbol: str,
        direction: str,
        volume: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "",
        magic: int = 777111,
    ) -> OrderResult:
        self.ticket_counter += 1
        ticket = self.ticket_counter
        # Synthetic fill price
        fill_price = 2000.0 if "XAU" in symbol else 1.10
        pos = PositionSnapshot(
            ticket=ticket,
            symbol=symbol,
            direction=direction.lower(),
            volume=volume,
            open_price=fill_price,
            current_price=fill_price,
            sl=sl,
            tp=tp,
            profit=0.0,
            magic=magic,
        )
        self.positions[ticket] = pos
        return OrderResult(success=True, ticket=ticket, price=fill_price, comment="FIX Fill (Tag 35=8)")

    def close_position(self, ticket: int, volume: Optional[float] = None) -> OrderResult:
        if ticket in self.positions:
            pos = self.positions.pop(ticket)
            self.balance += pos.profit
            return OrderResult(success=True, ticket=ticket, comment="FIX Order Cancelled/Closed")
        return OrderResult(success=False, comment="Position not found")

    def modify_position(
        self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None
    ) -> OrderResult:
        if ticket in self.positions:
            pos = self.positions[ticket]
            if sl is not None:
                pos.sl = sl
            if tp is not None:
                pos.tp = tp
            return OrderResult(success=True, ticket=ticket, comment="FIX Order Cancel/Replace (Tag 35=G)")
        return OrderResult(success=False, comment="Position not found")
