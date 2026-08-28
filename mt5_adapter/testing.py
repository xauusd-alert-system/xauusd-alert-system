"""Test doubles for the MT5 adapter (ТЗ 8.6).

:class:`MockMT5Module` implements the subset of the real MetaTrader5 module
surface used by the project (constants + functions), with configurable
responses per symbol and a call log, so adapter tests — and any module that
receives the client via DI — run without a Windows terminal.

The doubles use the same ``types.SimpleNamespace``-style plain attributes the
real namedtuples expose, so ``getattr(raw, "field", ...)`` access works.
"""

from __future__ import annotations

import threading
from collections import namedtuple
from typing import Any, Callable

# Module-level constants mirror the real package values.
TIMEFRAME_M1 = 1
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
TIMEFRAME_D1 = 16408

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6
ORDER_TIME_GTC = 0
ORDER_FILLING_IOC = 2
ORDER_FILLING_FOK = 0
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_REJECT = 10004

ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 1
ACCOUNT_TRADE_MODE_DEMO = 0

_TickTuple = namedtuple("_TickTuple", ["time", "bid", "ask", "last", "volume", "time_msc", "flags", "volume_real"])
_SymbolInfoTuple = namedtuple(
    "_SymbolInfoTuple",
    [
        "name",
        "digits",
        "point",
        "trade_tick_size",
        "trade_stops_level",
        "trade_freeze_level",
        "volume_min",
        "volume_max",
        "volume_step",
        "trade_contract_size",
        "trade_exec_mode",
        "visible",
    ],
)
_AccountTuple = namedtuple(
    "_AccountTuple",
    [
        "login",
        "balance",
        "equity",
        "margin",
        "margin_free",
        "currency",
        "trade_mode",
        "margin_mode",
        "leverage",
    ],
)
_PositionTuple = namedtuple(
    "_PositionTuple",
    [
        "ticket",
        "symbol",
        "type",
        "volume",
        "price_open",
        "price_current",
        "sl",
        "tp",
        "profit",
        "magic",
        "comment",
        "time",
    ],
)
_OrderResultTuple = namedtuple(
    "_OrderResultTuple",
    [
        "retcode",
        "deal",
        "order",
        "volume",
        "price",
        "comment",
        "request_id",
        "retcode_external",
    ],
)
_DealTuple = namedtuple(
    "_DealTuple",
    [
        "ticket",
        "order",
        "position_id",
        "symbol",
        "type",
        "entry",
        "volume",
        "price",
        "profit",
        "commission",
        "swap",
        "magic",
        "comment",
        "time",
    ],
)


class MockMT5Module:
    # Module-surface constants, mirrored as class attributes so
    # ``MT5Client.TIMEFRAME_M5``-style constant passthrough works.
    TIMEFRAME_M1 = TIMEFRAME_M1
    TIMEFRAME_M5 = TIMEFRAME_M5
    TIMEFRAME_M15 = TIMEFRAME_M15
    TIMEFRAME_M30 = TIMEFRAME_M30
    TIMEFRAME_H1 = TIMEFRAME_H1
    TIMEFRAME_H4 = TIMEFRAME_H4
    TIMEFRAME_D1 = TIMEFRAME_D1
    ORDER_TYPE_BUY = ORDER_TYPE_BUY
    ORDER_TYPE_SELL = ORDER_TYPE_SELL
    TRADE_ACTION_DEAL = TRADE_ACTION_DEAL
    TRADE_ACTION_SLTP = TRADE_ACTION_SLTP
    ORDER_TIME_GTC = ORDER_TIME_GTC
    ORDER_FILLING_IOC = ORDER_FILLING_IOC
    ORDER_FILLING_FOK = ORDER_FILLING_FOK
    TRADE_RETCODE_DONE = TRADE_RETCODE_DONE
    TRADE_RETCODE_REJECT = TRADE_RETCODE_REJECT
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING = ACCOUNT_MARGIN_MODE_RETAIL_NETTING
    ACCOUNT_TRADE_MODE_DEMO = ACCOUNT_TRADE_MODE_DEMO

    """Configurable in-memory MT5 module double.

    Example::

        mock = MockMT5Module()
        mock.set_tick("XAUUSD", bid=2400.10, ask=2400.40)
        mock.set_symbol_info("XAUUSD", digits=2, point=0.01)
        client = MT5Client(mt5_module=mock)
        client.initialize()
    """

    def __init__(
        self,
        login: int = 12345,
        balance: float = 10000.0,
        equity: float = 10000.0,
        currency: str = "USD",
        margin_mode: int = ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
    ):
        self._lock = threading.Lock()
        self._initialized = False
        self._last_error = (0, "no error")
        self._calls: list[tuple[str, tuple, dict]] = []
        self._next_ticket = 1_000_000

        # Configurable state (public attributes, tests mutate freely).
        self.account = _AccountTuple(
            login=login,
            balance=balance,
            equity=equity,
            margin=0.0,
            margin_free=equity,
            currency=currency,
            trade_mode=trade_mode,
            margin_mode=margin_mode,
            leverage=100,
        )
        self.ticks: dict[str, Any] = {}
        self.symbol_infos: dict[str, Any] = {}
        self.positions: list[Any] = []
        self.orders: list[Any] = []
        self.deals: dict[int, list[Any]] = {}  # by position_id
        self.rates: dict[str, Any] = {}  # by symbol
        self.book: dict[str, list[Any]] = {}
        # order_send handler: return None => module failure; a result tuple is
        # passed through as-is (adapter checks the retcode).
        self.order_send_handler: Callable[[dict], Any] | None = None

    # ------------------------------------------------------------------
    # Test configuration helpers
    # ------------------------------------------------------------------

    def set_tick(self, symbol: str, bid: float, ask: float, time: int = 1_700_000_000, **extra: Any) -> None:
        self.ticks[symbol] = _TickTuple(
            time=time,
            bid=bid,
            ask=ask,
            last=(bid + ask) / 2,
            volume=0.0,
            time_msc=time * 1000,
            flags=0,
            volume_real=0.0,
            **extra,
        )

    def set_symbol_info(self, symbol: str, digits: int = 2, point: float = 0.01, **extra: Any) -> None:
        self.symbol_infos[symbol] = _SymbolInfoTuple(
            name=symbol,
            digits=digits,
            point=point,
            trade_tick_size=extra.pop("trade_tick_size", point),
            trade_stops_level=extra.pop("trade_stops_level", 0),
            trade_freeze_level=extra.pop("trade_freeze_level", 0),
            volume_min=extra.pop("volume_min", 0.01),
            volume_max=extra.pop("volume_max", 100.0),
            volume_step=extra.pop("volume_step", 0.01),
            trade_contract_size=extra.pop("trade_contract_size", 100.0),
            trade_exec_mode=extra.pop("trade_exec_mode", 1),
            visible=extra.pop("visible", True),
            **extra,
        )

    def add_position(
        self,
        symbol: str,
        ticket: int | None = None,
        type: int = 0,
        volume: float = 0.1,
        price_open: float = 2400.0,
        profit: float = 0.0,
        magic: int = 777111,
        sl: float = 0.0,
        tp: float = 0.0,
        comment: str = "",
    ) -> Any:
        pos = _PositionTuple(
            ticket=ticket or self._next_ticket,
            symbol=symbol,
            type=type,
            volume=volume,
            price_open=price_open,
            price_current=price_open,
            sl=sl,
            tp=tp,
            profit=profit,
            magic=magic,
            comment=comment,
            time=1_700_000_000,
        )
        with self._lock:
            self._next_ticket += 1
            self.positions.append(pos)
        return pos

    def set_last_error(self, code: int, message: str) -> None:
        self._last_error = (code, message)

    @property
    def calls(self) -> list[tuple[str, tuple, dict]]:
        with self._lock:
            return list(self._calls)

    def call_count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    # ------------------------------------------------------------------
    # MetaTrader5 module surface
    # ------------------------------------------------------------------

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        self._log("initialize", args, kwargs)
        self._initialized = True
        return True

    def shutdown(self) -> None:
        self._log("shutdown", (), {})
        self._initialized = False

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    def account_info(self) -> Any:
        self._log("account_info", (), {})
        return self.account if self._initialized else None

    def terminal_info(self) -> Any:
        self._log("terminal_info", (), {})
        if not self._initialized:
            return None
        return {"connected": True, "trade_allowed": True}

    def symbol_info(self, symbol: str) -> Any:
        self._log("symbol_info", (symbol,), {})
        return self.symbol_infos.get(symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        self._log("symbol_info_tick", (symbol,), {})
        return self.ticks.get(symbol)

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        self._log("symbol_select", (symbol, enable), {})
        return symbol in self.symbol_infos

    def positions_get(self, *args: Any, **kwargs: Any) -> Any:
        self._log("positions_get", args, kwargs)
        if not self._initialized:
            return None
        if "ticket" in kwargs:
            return tuple(p for p in self.positions if p.ticket == kwargs["ticket"])
        if "symbol" in kwargs:
            return tuple(p for p in self.positions if p.symbol == kwargs["symbol"])
        return tuple(self.positions)

    def orders_get(self, *args: Any, **kwargs: Any) -> Any:
        self._log("orders_get", args, kwargs)
        if not self._initialized:
            return None
        return tuple(self.orders)

    def history_deals_get(self, *args: Any, **kwargs: Any) -> Any:
        self._log("history_deals_get", args, kwargs)
        if not self._initialized:
            return None
        if "position" in kwargs:
            return tuple(self.deals.get(kwargs["position"], []))
        return tuple(d for ds in self.deals.values() for d in ds)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Any:
        self._log("copy_rates_from_pos", (symbol, timeframe, start_pos, count), {})
        rates = self.rates.get(symbol)
        if rates is None:
            return None
        return rates[start_pos : start_pos + count]

    def copy_rates_range(self, symbol: str, timeframe: int, date_from: Any, date_to: Any) -> Any:
        self._log("copy_rates_range", (symbol, timeframe, date_from, date_to), {})
        return self.rates.get(symbol)

    def order_send(self, request: dict) -> Any:
        self._log("order_send", (dict(request),), {})
        if self.order_send_handler is not None:
            return self.order_send_handler(request)
        return _OrderResultTuple(
            retcode=TRADE_RETCODE_DONE,
            deal=self._next_ticket,
            order=self._next_ticket,
            volume=request.get("volume", 0.0),
            price=request.get("price", 0.0),
            comment="",
            request_id=self._next_ticket,
            retcode_external=0,
        )

    def order_check(self, request: dict) -> Any:
        self._log("order_check", (dict(request),), {})
        return {"retcode": 0, "comment": "ok"}

    def market_book_add(self, symbol: str) -> bool:
        self._log("market_book_add", (symbol,), {})
        return symbol in self.book

    def market_book_get(self, symbol: str) -> Any:
        self._log("market_book_get", (symbol,), {})
        return tuple(self.book.get(symbol, []))

    def market_book_remove(self, symbol: str) -> bool:
        self._log("market_book_remove", (symbol,), {})
        return True

    # ------------------------------------------------------------------

    def _log(self, name: str, args: tuple, kwargs: dict) -> None:
        with self._lock:
            self._calls.append((name, args, kwargs))
