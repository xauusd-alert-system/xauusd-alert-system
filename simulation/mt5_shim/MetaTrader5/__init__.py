"""
Fake ``MetaTrader5`` package: an MT5 API shim that fronts the virtual market.

This package is injected onto ``sys.path`` (before the real MetaTrader5 pip
package, if any) by ``scripts/run_simulation.py`` so that ``import MetaTrader5
as mt5`` in the *unmodified* trading code (``data/mt5_provider.py``,
``execution/mt5_trader.py``, ``execution/risk_manager.py``) resolves to this
object instead of the real terminal API.

The shim dispatches every call onto ``simulation.virtual_state.VirtualState``
(account/positions/deals) and ``simulation.simulator.MarketSimulator``
(market data / prices / M5 bars).  It is a *duck-typed* replacement: it
returns lightweight dataclasses / numpy structured arrays whose fields match
the attributes the real code expects.

Injection::

    from simulation.mt5_shim import MetaTrader5 as mt5
    mt5._inject(state, simulator, cfg)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

import numpy as np

# ----------------------------------------------------------------------
# Constants (values match the real MetaTrader5 Python package).
# ----------------------------------------------------------------------

# Return codes
TRADE_RETCODE_DONE: int = 10009
TRADE_RETCODE_REQ_REJECT: int = 10002
TRADE_RETCODE_INVALID_REQUEST: int = 10014
TRADE_RETCODE_INTERNAL_ERROR: int = 10019

# Trade actions
TRADE_ACTION_DEAL: int = 1
TRADE_ACTION_SLTP: int = 6

# Order types
ORDER_TYPE_BUY: int = 0
ORDER_TYPE_SELL: int = 1

# Order time / filling
ORDER_TIME_GTC: int = 0
ORDER_TIME_DAY: int = 1
ORDER_TIME_SPECIFIED: int = 2
ORDER_FILLING_FOK: int = 0
ORDER_FILLING_IOC: int = 1
ORDER_FILLING_RETURN: int = 2

# Timeframes
TIMEFRAME_M1: int = 1
TIMEFRAME_M5: int = 5
TIMEFRAME_M15: int = 15
TIMEFRAME_M30: int = 30
TIMEFRAME_H1: int = 60
TIMEFRAME_H4: int = 240
TIMEFRAME_D1: int = 1440

# Deal entries
DEAL_ENTRY_IN: int = 0
DEAL_ENTRY_OUT: int = 1
DEAL_ENTRY_INOUT: int = 2

# Position types matching the real ORDER_TYPE_* mapping.
POSITION_TYPE_BUY: int = 0
POSITION_TYPE_SELL: int = 1


# ----------------------------------------------------------------------
# Module-level state (set via _inject()).
# ----------------------------------------------------------------------

_STATE = None  # VirtualState
_SIMULATOR = None  # MarketSimulator
_SIM_CFG: dict = {}
_SYMBOL_TIMEFRAME_TICKS = {
    TIMEFRAME_M1: 12,
    TIMEFRAME_M5: 60,
    TIMEFRAME_M15: 180,
    TIMEFRAME_H1: 720,
}


def _inject(state, simulator, cfg: dict) -> None:
    """Wire the shim to a VirtualState + MarketSimulator instance."""
    global _STATE, _SIMULATOR, _SIM_CFG
    _STATE = state
    _SIMULATOR = simulator
    _SIM_CFG = cfg or {}


# ----------------------------------------------------------------------
# Session / init helpers
# ----------------------------------------------------------------------


def initialize(*args, **kwargs) -> bool:
    """Report a successful connection to the virtual terminal."""
    return True


def shutdown(*args, **kwargs) -> bool:
    """Stop the virtual terminal (no-op in the shim)."""
    return True


def last_error() -> tuple[int, str]:
    """Return (code, message) mirroring the real API signature."""
    return (0, "Virtual MT5 ok")


def version(*args, **kwargs) -> tuple[int, int, int]:
    return (5, 0, 0)


def terminal_info(*args, **kwargs) -> dict:
    return {
        "name": "Virtual Terminal",
        "version": "5.0.0",
        "path": "",
        "data_path": "",
        "connected": True,
    }


def account_info() -> Optional[object]:
    """Return a VirtualAccountInfo with up-to-date equity."""
    if _STATE is None:
        return None
    return _STATE.account_info()


def symbol_info(symbol: str) -> Optional[object]:
    """Return VirtualSymbolInfo for ``symbol`` (fallback to XAUUSD)."""
    if _STATE is None:
        return None
    info = _STATE.symbol_info(symbol)
    return info


def symbol_info_tick(symbol: str) -> Optional[object]:
    """Return a VirtualTick with the current simulated bid/ask."""
    if _SIMULATOR is None:
        return None
    info = _STATE.symbol_info(symbol) if _STATE is not None else None
    digits = info.digits if info else 2
    point = info.point if info else 0.01
    bid = round(_SIMULATOR.current_bid, digits)
    ask = round(_SIMULATOR.current_ask, digits)
    spread = round(ask - bid, digits)
    if info is not None:
        info.spread = spread
        info.tick_size = point
    return _STATE.make_tick(symbol, bid, ask, t=int(_SIMULATOR.tick))


def symbol_select(*args, **kwargs) -> bool:
    """Pretend the symbol is always enabled in Market Watch."""
    return True


def symbol_info_get(symbol: str) -> Optional[object]:
    return symbol_info(symbol)


def symbols_total(*args, **kwargs) -> int:
    return 0


def symbols_get(*args, **kwargs) -> tuple:
    return ()


# ----------------------------------------------------------------------
# Market data (candles)
# ----------------------------------------------------------------------


def _timestamp_now() -> int:
    """Current Unix seconds (UTC), used to anchor newly-closed bars."""
    return int(datetime.now(UTC).timestamp())


def copy_rates_from_pos(
    symbol: str,
    timeframe: int,
    start_pos: int,
    count: int,
) -> Optional[np.ndarray]:
    """Return bars rigidly mirroring MT5's numpy structured array.

    Fields: time(int), open, high, low, close, tick_volume, spread,
    real_volume.  Bars come from the simulator's OHLCV aggregator, so
    ``data/mt5_provider._normalize_rates`` (which reads df["time"] and
    renames tick_volume->volume) works unchanged.
    """
    if _SIMULATOR is None or _STATE is None:
        return None

    interval_ticks = _SYMBOL_TIMEFRAME_TICKS.get(int(timeframe), 60)
    try:
        df = _SIMULATOR.aggregator.get_bars_by_interval(interval_ticks, n=max(int(count) + int(start_pos), 1))
    except Exception:
        return None

    if df is None or len(df) == 0:
        return None

    total = len(df)
    if start_pos >= total:
        return None

    df = df.iloc[start_pos:total].head(count) if count > 0 else df.iloc[start_pos:total]

    dtype = np.dtype(
        [
            ("time", "<i8"),
            ("open", "<f8"),
            ("high", "<f8"),
            ("low", "<f8"),
            ("close", "<f8"),
            ("tick_volume", "<i8"),
            ("spread", "<i4"),
            ("real_volume", "<i8"),
        ]
    )
    rows = []
    for _, r in df.iterrows():
        rows.append(
            (
                int(r["timestamp"]),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                max(1, int(round(float(r["volume"])))),
                0,
                max(1, int(round(float(r["volume"])))),
            )
        )
    if not rows:
        return None
    return np.array(rows, dtype=dtype)


def copy_rates_range(
    symbol: str,
    timeframe: int,
    date_from,
    date_to,
) -> Optional[np.ndarray]:
    """Return bars within [date_from, date_to] (ignores range, returns tail)."""
    if _SIMULATOR is None:
        return None
    interval_ticks = _SYMBOL_TIMEFRAME_TICKS.get(int(timeframe), 60)
    try:
        df = _SIMULATOR.aggregator.get_bars_by_interval(interval_ticks, n=5000)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    start = int(pd_timestamp(date_from).timestamp()) if date_from else 0
    end = int(pd_timestamp(date_to).timestamp()) if date_to else (1 << 62)
    df = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)]
    if len(df) == 0:
        return None
    return _df_to_rates_array(df)


def _df_to_rates_array(df) -> np.ndarray:
    dtype = np.dtype(
        [
            ("time", "<i8"),
            ("open", "<f8"),
            ("high", "<f8"),
            ("low", "<f8"),
            ("close", "<f8"),
            ("tick_volume", "<i8"),
            ("spread", "<i4"),
            ("real_volume", "<i8"),
        ]
    )
    rows = []
    for _, r in df.iterrows():
        vol = max(1, int(round(float(r["volume"]))))
        rows.append(
            (
                int(r["timestamp"]),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                vol,
                0,
                vol,
            )
        )
    if not rows:
        return None
    return np.array(rows, dtype=dtype)


def pd_timestamp(value) -> datetime:
    """Coerce a pandas Timestamp/datetime/number to a naive UTC datetime."""
    import pandas as pd

    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").tz_localize(None).to_pydatetime()


# ----------------------------------------------------------------------
# Orders / positions / history
# ----------------------------------------------------------------------


def positions_get(
    symbol: Optional[str] = None,
    group: Optional[str] = None,
    ticket: Optional[int] = None,
    *args,
    **kwargs,
) -> Optional[list]:
    """Return open positions, mirroring the REAL MetaTrader5 Python API.

    The real API accepts only `symbol`, `group` and `ticket` — there is NO
    `magic` parameter. This shim previously accepted `magic` and filtered on it,
    which let production code call `positions_get(magic=...)` that would raise
    TypeError on a live terminal (audit N3/W9). To keep the shim an honest test
    double we now match the real signature and explicitly reject `magic`, so any
    production code that regresses to the old call fails here exactly as it
    would on a real terminal. Callers must filter by `pos.magic` in Python (see
    execution.mt5_trader.positions_get_by_magic).
    """
    if "magic" in kwargs:
        raise TypeError(
            "positions_get() got an unexpected keyword argument 'magic' "
            "(real MT5 API: symbol/group/ticket only; filter by pos.magic instead)"
        )
    if _STATE is None:
        return None
    if symbol is None and group is None and ticket is None:
        positions = _STATE.get_positions()
    elif ticket is not None:
        pos = _STATE.get_position(int(ticket))
        positions = [pos] if pos is not None else []
    else:
        positions = _STATE.get_positions(symbol=symbol)
    return positions or None


def history_deals_get(
    position: Optional[int] = None,
    *args,
    **kwargs,
) -> Optional[list]:
    """Return deal history (optionally filtered by position ticket)."""
    if _STATE is None:
        return None
    deals = _STATE.get_history_deals(position=position)
    return deals or None


def order_send(request: dict) -> object:
    """Execute a trade request against the virtual account.

    Supports the two actions used by the real code:
      - TRADE_ACTION_SLTP  (modify SL/TP on an existing position)
      - TRADE_ACTION_DEAL  (open a new position, or close by "position" id)
    Returns a VirtualOrderResult with retcode TRADE_RETCODE_DONE on success.
    """
    if _STATE is None or _SIMULATOR is None:
        return _result(
            TRADE_RETCODE_REQ_REJECT,
            0,
            "Virtual MT5 not injected",
        )

    from simulation.virtual_state import (
        TRADE_RETCODE_DONE as V_DONE,
    )
    from simulation.virtual_state import (
        VirtualOrderResult,
    )

    action = request.get("action")
    magic = int(request.get("magic", 0) or 0)
    symbol = request.get("symbol", "")
    comment = request.get("comment", "virtual")
    t = int(_SIMULATOR.tick)

    if action == TRADE_ACTION_SLTP:
        position_ticket = int(request.get("position", 0))
        pos = _STATE.get_position(position_ticket)
        if pos is None:
            return VirtualOrderResult(
                retcode=TRADE_RETCODE_INVALID_REQUEST,
                order=0,
                comment="No such position",
            )
        sl = float(request.get("sl", 0.0) or 0.0)
        tp = float(request.get("tp", 0.0) or 0.0)
        _STATE.modify_sl_tp(pos, sl, tp)
        # Real MT5 returns a distinct order ticket in the "order" field of
        # order_send() results (this acts like a request/ack id). Do NOT use
        # the position ticket here so callers cannot blindly treat result.order
        # as a position id -- they must resolve positions via positions_get().
        return VirtualOrderResult(
            retcode=TRADE_RETCODE_DONE,
            order=_STATE._next_order_ticket(),
            deal=0,
            comment="SL/TP modified",
        )

    if action == TRADE_ACTION_DEAL:
        order_type = int(request.get("type", ORDER_TYPE_BUY))
        position = int(request.get("position", 0) or 0)
        volume = float(request.get("volume", 0.0))

        # --- Close an existing position (a "position" id means reversal) ---
        if position != 0:
            pos = _STATE.get_position(position)
            if pos is None:
                return VirtualOrderResult(
                    retcode=TRADE_RETCODE_INVALID_REQUEST,
                    order=0,
                    comment="No such open position to close",
                )
            price = float(request.get("price") or _SIMULATOR.current_bid)
            out_deal = _STATE.close_partial(pos, volume, price, comment=comment, t=t)
            if out_deal is None:
                return VirtualOrderResult(
                    retcode=TRADE_RETCODE_INVALID_REQUEST,
                    order=0,
                    comment="Close failed",
                )
            # Real MT5: result.order is the order ticket, NOT the position
            # ticket. The caller must resolve the position via positions_get().
            return VirtualOrderResult(
                retcode=V_DONE,
                order=_STATE._next_order_ticket(),
                deal=out_deal.ticket,
                comment=comment,
                price=round(price, 6),
                volume=round(volume, 2),
            )

        # --- Open a new position ---
        info = _STATE.symbol_info(symbol)
        if info is None:
            return VirtualOrderResult(
                retcode=TRADE_RETCODE_INVALID_REQUEST,
                order=0,
                comment=f"Unknown symbol {symbol}",
            )
        # Normalize volume to the symbol step (round to 2 decimals safely).
        step = info.volume_step
        volume = round(round(volume / step) * step, 2)
        volume = max(info.volume_min, volume)

        position_type = order_type  # BUY=0 / SELL=1 as-is
        if position_type == ORDER_TYPE_BUY:
            price = float(request.get("price") or _SIMULATOR.current_ask)
        else:
            price = float(request.get("price") or _SIMULATOR.current_bid)

        sl = float(request.get("sl", 0.0) or 0.0)
        tp = float(request.get("tp", 0.0) or 0.0)

        pos, deal = _STATE.open_position(
            symbol=symbol,
            position_type=position_type,
            volume=volume,
            price=price,
            sl=sl,
            tp=tp,
            magic=magic,
            comment=comment,
            t=t,
        )
        # Ticket-faithful to real MT5: `order` is a *distinct* order ticket
        # (starting at 1), NOT the position ticket (100001+). Production code
        # (mt5_trader.execute_signal HIGH 22) therefore MUST resolve the real
        # position ticket via positions_get() instead of assuming
        # result.order == pos.ticket -- exactly as in a live terminal.
        return VirtualOrderResult(
            retcode=V_DONE,
            order=_STATE._next_order_ticket(),
            deal=deal.ticket,
            comment=comment,
            price=round(price, 6),
            volume=round(volume, 2),
        )

    return VirtualOrderResult(
        retcode=TRADE_RETCODE_INVALID_REQUEST,
        order=0,
        comment=f"Unsupported action {action}",
    )


def _result(retcode: int, order: int, comment: str) -> object:
    from simulation.virtual_state import VirtualOrderResult

    return VirtualOrderResult(retcode=retcode, order=order, comment=comment)


def positions_total(*args, **kwargs) -> int:
    return 0


def orders_total(*args, **kwargs) -> int:
    return 0


def orders_get(*args, **kwargs) -> Optional[list]:
    return None


def history_orders_total(*args, **kwargs) -> int:
    return 0


def history_orders_get(*args, **kwargs) -> Optional[list]:
    return None


def history_deals_total(*args, **kwargs) -> int:
    return 0


def market_book_get(*args, **kwargs) -> Optional[list]:
    return None


__all__ = [
    "TRADE_RETCODE_DONE",
    "TRADE_RETCODE_REQ_REJECT",
    "TRADE_RETCODE_INVALID_REQUEST",
    "TRADE_RETCODE_INTERNAL_ERROR",
    "TRADE_ACTION_DEAL",
    "TRADE_ACTION_SLTP",
    "ORDER_TYPE_BUY",
    "ORDER_TYPE_SELL",
    "ORDER_TIME_GTC",
    "ORDER_TIME_DAY",
    "ORDER_TIME_SPECIFIED",
    "ORDER_FILLING_FOK",
    "ORDER_FILLING_IOC",
    "ORDER_FILLING_RETURN",
    "TIMEFRAME_M1",
    "TIMEFRAME_M5",
    "TIMEFRAME_M15",
    "TIMEFRAME_M30",
    "TIMEFRAME_H1",
    "TIMEFRAME_H4",
    "TIMEFRAME_D1",
    "POSITION_TYPE_BUY",
    "POSITION_TYPE_SELL",
    "initialize",
    "shutdown",
    "last_error",
    "version",
    "terminal_info",
    "account_info",
    "symbol_info",
    "symbol_info_tick",
    "symbol_select",
    "symbol_info_get",
    "symbols_total",
    "symbols_get",
    "copy_rates_from_pos",
    "copy_rates_range",
    "positions_get",
    "history_deals_get",
    "order_send",
    "positions_total",
    "orders_total",
    "orders_get",
    "history_orders_total",
    "history_orders_get",
    "history_deals_total",
    "market_book_get",
    "_inject",
]
