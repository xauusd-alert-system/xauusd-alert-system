"""mt5_adapter — the single access point to MetaTrader5 (ТЗ 8.6).

All direct ``import MetaTrader5`` usage lives inside this package only; the
rest of the codebase depends on :class:`MT5Client` (or on injected test
doubles from :mod:`mt5_adapter.testing`), enforced by the guard test in
``mt5_adapter/tests/test_no_direct_mt5_calls.py``.
"""
from mt5_adapter.cache import SymbolCache
from mt5_adapter.client import MT5Client, TRADE_RETCODE_DONE
from mt5_adapter.errors import (
    MT5AdapterError,
    MT5CallError,
    MT5NotInitializedError,
    MT5OrderRejectedError,
    MT5RateLimitedError,
    MT5TimeoutError,
)
from mt5_adapter.rate_limiter import MT5RateLimiter
from mt5_adapter.types import (
    AccountInfo,
    DealInfo,
    OrderResult,
    PositionInfo,
    SymbolInfo,
    Tick,
)

__all__ = [
    "MT5Client",
    "TRADE_RETCODE_DONE",
    "MT5RateLimiter",
    "SymbolCache",
    "MT5AdapterError",
    "MT5CallError",
    "MT5NotInitializedError",
    "MT5OrderRejectedError",
    "MT5RateLimitedError",
    "MT5TimeoutError",
    "Tick",
    "SymbolInfo",
    "AccountInfo",
    "PositionInfo",
    "OrderResult",
    "DealInfo",
]
