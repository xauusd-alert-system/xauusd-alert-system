"""Typed mirror structures for the raw MetaTrader5 namedtuples (ТЗ 8.6).

MT5's Python API returns plain namedtuples. The adapter returns them as-is by
default (backward compatibility with existing execution code), but the
dataclasses below are available for typed conversions via ``.from_raw()`` /
``.raw()`` round-trips.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tick:
    """Mirror of ``MetaTrader5.symbol_info_tick`` output."""

    time: int = 0
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: float = 0.0
    time_msc: int = 0
    flags: int = 0
    volume_real: float = 0.0

    @classmethod
    def from_raw(cls, raw: Any) -> "Tick":
        if raw is None:
            raise ValueError("cannot build Tick from None")
        return cls(
            time=int(getattr(raw, "time", 0) or 0),
            bid=float(getattr(raw, "bid", 0.0) or 0.0),
            ask=float(getattr(raw, "ask", 0.0) or 0.0),
            last=float(getattr(raw, "last", 0.0) or 0.0),
            volume=float(getattr(raw, "volume", 0.0) or 0.0),
            time_msc=int(getattr(raw, "time_msc", 0) or 0),
            flags=int(getattr(raw, "flags", 0) or 0),
            volume_real=float(getattr(raw, "volume_real", 0.0) or 0.0),
        )


@dataclass(frozen=True)
class SymbolInfo:
    """Mirror of the subset of ``MetaTrader5.symbol_info`` the project uses."""

    name: str = ""
    digits: int = 0
    point: float = 0.0
    trade_tick_size: float = 0.0
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    volume_min: float = 0.0
    volume_max: float = 0.0
    volume_step: float = 0.0
    trade_contract_size: float = 0.0
    trade_exec_mode: Any = None
    visible: bool = False

    @classmethod
    def from_raw(cls, raw: Any) -> "SymbolInfo":
        if raw is None:
            raise ValueError("cannot build SymbolInfo from None")
        return cls(
            name=str(getattr(raw, "name", "") or ""),
            digits=int(getattr(raw, "digits", 0) or 0),
            point=float(getattr(raw, "point", 0.0) or 0.0),
            trade_tick_size=float(getattr(raw, "trade_tick_size", 0.0) or 0.0),
            trade_stops_level=int(getattr(raw, "trade_stops_level", 0) or 0),
            trade_freeze_level=int(getattr(raw, "trade_freeze_level", 0) or 0),
            volume_min=float(getattr(raw, "volume_min", 0.0) or 0.0),
            volume_max=float(getattr(raw, "volume_max", 0.0) or 0.0),
            volume_step=float(getattr(raw, "volume_step", 0.0) or 0.0),
            trade_contract_size=float(
                getattr(raw, "trade_contract_size", 0.0) or 0.0),
            trade_exec_mode=getattr(raw, "trade_exec_mode", None),
            visible=bool(getattr(raw, "visible", False)),
        )


@dataclass(frozen=True)
class AccountInfo:
    """Mirror of the subset of ``MetaTrader5.account_info`` the project uses."""

    login: int = 0
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    currency: str = "USD"
    trade_mode: int = -1
    margin_mode: int = -1
    leverage: int = 0

    @classmethod
    def from_raw(cls, raw: Any) -> "AccountInfo":
        if raw is None:
            raise ValueError("cannot build AccountInfo from None")
        return cls(
            login=int(getattr(raw, "login", 0) or 0),
            balance=float(getattr(raw, "balance", 0.0) or 0.0),
            equity=float(getattr(raw, "equity", 0.0) or 0.0),
            margin=float(getattr(raw, "margin", 0.0) or 0.0),
            margin_free=float(getattr(raw, "margin_free", 0.0) or 0.0),
            currency=str(getattr(raw, "currency", "USD") or "USD"),
            trade_mode=int(getattr(raw, "trade_mode", -1) if
                           getattr(raw, "trade_mode", None) is not None else -1),
            margin_mode=int(getattr(raw, "margin_mode", -1) if
                            getattr(raw, "margin_mode", None) is not None else -1),
            leverage=int(getattr(raw, "leverage", 0) or 0),
        )


@dataclass(frozen=True)
class PositionInfo:
    """Mirror of the subset of ``MetaTrader5.positions_get`` items."""

    ticket: int = 0
    symbol: str = ""
    type: int = 0
    volume: float = 0.0
    price_open: float = 0.0
    price_current: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0
    magic: int = 0
    comment: str = ""
    time: int = 0

    @classmethod
    def from_raw(cls, raw: Any) -> "PositionInfo":
        if raw is None:
            raise ValueError("cannot build PositionInfo from None")
        return cls(
            ticket=int(getattr(raw, "ticket", 0) or 0),
            symbol=str(getattr(raw, "symbol", "") or ""),
            type=int(getattr(raw, "type", 0) or 0),
            volume=float(getattr(raw, "volume", 0.0) or 0.0),
            price_open=float(getattr(raw, "price_open", 0.0) or 0.0),
            price_current=float(getattr(raw, "price_current", 0.0) or 0.0),
            sl=float(getattr(raw, "sl", 0.0) or 0.0),
            tp=float(getattr(raw, "tp", 0.0) or 0.0),
            profit=float(getattr(raw, "profit", 0.0) or 0.0),
            magic=int(getattr(raw, "magic", 0) or 0),
            comment=str(getattr(raw, "comment", "") or ""),
            time=int(getattr(raw, "time", 0) or 0),
        )


@dataclass(frozen=True)
class OrderResult:
    """Mirror of ``MetaTrader5.order_send`` result."""

    retcode: int = -1
    deal: int = 0
    order: int = 0
    volume: float = 0.0
    price: float = 0.0
    comment: str = ""
    request_id: int = 0
    retcode_external: int = 0

    @property
    def ok(self) -> bool:
        # The terminal's success code for TRADE_ACTION_DEAL / SLTP is 10009
        # (TRADE_RETCODE_DONE). The adapter treats exactly DONE as success.
        return self.retcode == 10009

    @classmethod
    def from_raw(cls, raw: Any) -> "OrderResult":
        if raw is None:
            raise ValueError("cannot build OrderResult from None")
        return cls(
            retcode=int(getattr(raw, "retcode", -1) if
                        getattr(raw, "retcode", None) is not None else -1),
            deal=int(getattr(raw, "deal", 0) or 0),
            order=int(getattr(raw, "order", 0) or 0),
            volume=float(getattr(raw, "volume", 0.0) or 0.0),
            price=float(getattr(raw, "price", 0.0) or 0.0),
            comment=str(getattr(raw, "comment", "") or ""),
            request_id=int(getattr(raw, "request_id", 0) or 0),
            retcode_external=int(getattr(raw, "retcode_external", 0) or 0),
        )


@dataclass(frozen=True)
class DealInfo:
    """Mirror of ``MetaTrader5.history_deals_get`` items."""

    ticket: int = 0
    order: int = 0
    position_id: int = 0
    symbol: str = ""
    type: int = -1
    entry: int = -1
    volume: float = 0.0
    price: float = 0.0
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    magic: int = 0
    comment: str = ""
    time: int = 0

    @classmethod
    def from_raw(cls, raw: Any) -> "DealInfo":
        if raw is None:
            raise ValueError("cannot build DealInfo from None")
        return cls(
            ticket=int(getattr(raw, "ticket", 0) or 0),
            order=int(getattr(raw, "order", 0) or 0),
            position_id=int(getattr(raw, "position_id", 0) or 0),
            symbol=str(getattr(raw, "symbol", "") or ""),
            type=int(getattr(raw, "type", -1)),
            entry=int(getattr(raw, "entry", -1)),
            volume=float(getattr(raw, "volume", 0.0) or 0.0),
            price=float(getattr(raw, "price", 0.0) or 0.0),
            profit=float(getattr(raw, "profit", 0.0) or 0.0),
            commission=float(getattr(raw, "commission", 0.0) or 0.0),
            swap=float(getattr(raw, "swap", 0.0) or 0.0),
            magic=int(getattr(raw, "magic", 0) or 0),
            comment=str(getattr(raw, "comment", "") or ""),
            time=int(getattr(raw, "time", 0) or 0),
        )
