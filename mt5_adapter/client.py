"""MT5Client — the single access point to MetaTrader5 (ТЗ 8.6).

Design:

* The low-level MT5 module is injected via the ``mt5_module`` constructor
  parameter (dependency injection). When omitted, the real ``MetaTrader5``
  package is imported lazily on first use, so importing this module never
  requires a Windows terminal and unit tests can pass a mock.
* Every public method (a) takes a rate-limiter slot, (b) increments the
  per-method call counters (``calls`` / ``calls_per_poll`` compatible with the
  existing poll diagnostics), (c) translates raw failures (``None`` returns /
  initialize returning False) into adapter exceptions.
* Return values stay the raw MT5 namedtuples for backward compatibility with
  the existing execution modules; the typed mirrors in ``types.py`` are
  opt-in.
* Constants (``TIMEFRAME_M5``, ``TRADE_ACTION_DEAL``, ``ORDER_TYPE_BUY``, ...)
  are exposed as attributes so calling code can keep reading them off the
  client instead of importing the raw package.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

from mt5_adapter.cache import SymbolCache
from mt5_adapter.errors import (
    MT5AdapterError,
    MT5CallError,
    MT5NotInitializedError,
    MT5OrderRejectedError,
)
from mt5_adapter.rate_limiter import MT5RateLimiter

logger = logging.getLogger("mt5_adapter")

# The terminal's success retcode for TRADE_ACTION_DEAL / TRADE_ACTION_SLTP.
TRADE_RETCODE_DONE = 10009

# Methods counted as part of one polling cycle (used by the live-loop
# diagnostics; mirrors the old calls_per_poll bookkeeping).
_POLL_METHODS = frozenset({
    "account_info", "symbol_info", "symbol_info_tick",
    "symbol_info_tick_cached", "symbol_info_cached",
    "positions_get", "orders_get", "copy_rates_from_pos",
})

_DEFAULT_MAX_CPS = 10


def _load_real_mt5_module() -> Any:
    """Lazy import of the real (or shim-injected) MetaTrader5 package.

    Raises ``MT5NotInitializedError`` when the package is unavailable so the
    failure carries adapter semantics instead of a bare ImportError."""
    from mt5_adapter.lazy import get_mt5_module
    try:
        return get_mt5_module()
    except ImportError as exc:
        raise MT5NotInitializedError(
            f"MetaTrader5 package is not importable: {exc}") from exc


def _max_calls_per_second_from_env(default: int | None = None) -> int | None:
    """Resolve the rate limit from MT5_RATE_LIMIT_MAX_CPS (0 = disabled)."""
    raw = os.getenv("MT5_RATE_LIMIT_MAX_CPS")
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid MT5_RATE_LIMIT_MAX_CPS=%r; ignoring", raw)
        return default
    if value <= 0:
        return None  # explicitly disabled
    return value


class MT5Client:
    """Unified MT5 access point with rate limiting, caching and call counts.

    Args:
        mt5_module: the low-level module (real MetaTrader5 or a test double).
            ``None`` triggers a lazy import on first use.
        rate_limiter: optional :class:`MT5RateLimiter`. When ``None`` one is
            built from the ``MT5_RATE_LIMIT_MAX_CPS`` env var (default:
            disabled — no behaviour change for existing tests/paper runs).
        cache: optional :class:`SymbolCache` for the ``*_cached`` methods.
        timeout_s: soft timeout budget per call (raised as
            :class:`MT5TimeoutError`); ``None`` disables the check.
    """

    def __init__(
        self,
        mt5_module: Any = None,
        rate_limiter: MT5RateLimiter | None = None,
        cache: SymbolCache | None = None,
        timeout_s: float | None = None,
    ):
        self._mt5 = mt5_module
        self._mt5_loaded = mt5_module is not None
        self._module_lock = threading.Lock()
        if rate_limiter is None:
            max_cps = _max_calls_per_second_from_env(default=None)
            rate_limiter = (MT5RateLimiter(max_calls_per_second=max_cps)
                            if max_cps else MT5RateLimiter(None))
        self.rate_limiter = rate_limiter
        self.cache = cache if cache is not None else SymbolCache(ttl_ms=500)
        self.timeout_s = timeout_s

        # Call counters: total per method + per-poll-cycle rollup.
        self.calls: dict[str, int] = {}
        self.calls_total = 0
        self.calls_per_poll = 0
        self._counter_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Module access / lifecycle
    # ------------------------------------------------------------------

    @property
    def module(self) -> Any:
        """The injected (or lazily imported) low-level module."""
        if not self._mt5_loaded:
            with self._module_lock:
                if not self._mt5_loaded:
                    self._mt5 = _load_real_mt5_module()
                    self._mt5_loaded = True
        return self._mt5

    def __getattr__(self, name: str) -> Any:
        """Expose MT5 constants (TIMEFRAME_M5, TRADE_ACTION_DEAL, ...) without
        importing the raw package at call sites."""
        try:
            return getattr(self.module, name)
        except AttributeError:
            raise AttributeError(
                f"MT5Client and the underlying MT5 module have no "
                f"attribute {name!r}") from None

    def _call(self, method: str, func: Callable[[], Any],
              ok_when: Callable[[Any], bool] | None = None,
              error_context: Callable[[], str] | None = None) -> Any:
        """Rate-limited, counted, error-wrapping invocation of ``method``."""
        self.rate_limiter.wait()
        started = None
        if self.timeout_s is not None:
            started = time.monotonic()
        try:
            result = func()
        except MT5AdapterError:
            raise
        except Exception as exc:
            self._count(method)
            raise MT5CallError(
                f"{method} raised {type(exc).__name__}: {exc}",
                comment=str(exc)) from exc

        self._count(method)

        if ok_when is not None:
            if not ok_when(result):
                last_error = self._last_error_text()
                if result is None:
                    raise MT5NotInitializedError(
                        f"{method} returned None. {last_error}")
                raise MT5CallError(
                    f"{method} failed. {last_error}", comment=last_error)
        elif result is None:
            raise MT5NotInitializedError(
                f"{method} returned None. {self._last_error_text()}")

        if started is not None:
            elapsed = time.monotonic() - started
            if elapsed > self.timeout_s:
                from mt5_adapter.errors import MT5TimeoutError
                raise MT5TimeoutError(
                    f"{method} took {elapsed:.3f}s > timeout {self.timeout_s}s")
        return result

    def _count(self, method: str) -> None:
        with self._counter_lock:
            self.calls[method] = self.calls.get(method, 0) + 1
            self.calls_total += 1
            if method in _POLL_METHODS:
                self.calls_per_poll += 1

    def reset_poll_counters(self) -> None:
        """Reset the per-poll rollup (call at the start of each poll cycle)."""
        with self._counter_lock:
            self.calls_per_poll = 0

    def _last_error_text(self) -> str:
        try:
            last_error = self.module.last_error()
            if isinstance(last_error, (tuple, list)) and last_error:
                code, message = (list(last_error) + [None, None])[:2]
                return f"last_error=({code}, {message})"
            return f"last_error={last_error!r}"
        except Exception:  # pragma: no cover — mock without last_error
            return "last_error unavailable"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        """Initialize the terminal connection. Raises MT5CallError when the
        terminal refuses (initialize() returned False)."""
        def _ok(result: Any) -> bool:
            return bool(result)

        def _err() -> str:
            return f"initialize failed. {self._last_error_text()}"

        result = self._call("initialize",
                            lambda: self.module.initialize(*args, **kwargs),
                            ok_when=_ok, error_context=_err)
        return bool(result)

    def shutdown(self) -> None:
        self._call("shutdown", lambda: self.module.shutdown(),
                   ok_when=lambda _r: True)

    # ------------------------------------------------------------------
    # Read-only market/account data
    # ------------------------------------------------------------------

    def account_info(self) -> Any:
        return self._call("account_info", self.module.account_info)

    def terminal_info(self) -> Any:
        return self._call("terminal_info", self.module.terminal_info)

    def symbol_info(self, symbol: str) -> Any:
        return self._call("symbol_info", lambda: self.module.symbol_info(symbol))

    def symbol_info_cached(self, symbol: str) -> Any:
        return self.cache.get_or_fetch(
            ("symbol_info", symbol),
            lambda: self._call("symbol_info", lambda: self.module.symbol_info(symbol)),
        )

    def symbol_info_tick(self, symbol: str) -> Any:
        return self._call("symbol_info_tick",
                          lambda: self.module.symbol_info_tick(symbol))

    def symbol_info_tick_cached(self, symbol: str) -> Any:
        return self.cache.get_or_fetch(
            ("symbol_info_tick", symbol),
            lambda: self._call("symbol_info_tick",
                               lambda: self.module.symbol_info_tick(symbol)),
        )

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        result = self._call(
            "symbol_select",
            lambda: self.module.symbol_select(symbol, enable),
            ok_when=lambda r: bool(r),
        )
        return bool(result)

    def positions_get(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("positions_get",
                          lambda: self.module.positions_get(*args, **kwargs))

    def orders_get(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("orders_get",
                          lambda: self.module.orders_get(*args, **kwargs))

    def history_deals_get(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("history_deals_get",
                          lambda: self.module.history_deals_get(*args, **kwargs))

    def history_orders_get(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("history_orders_get",
                          lambda: self.module.history_orders_get(*args, **kwargs))

    def copy_rates_from_pos(self, symbol: str, timeframe: int,
                            start_pos: int, count: int) -> Any:
        return self._call(
            "copy_rates_from_pos",
            lambda: self.module.copy_rates_from_pos(
                symbol, timeframe, start_pos, count),
            ok_when=lambda r: r is not None and len(r) > 0,
        )

    def copy_rates_range(self, symbol: str, timeframe: int,
                         date_from: Any, date_to: Any) -> Any:
        return self._call(
            "copy_rates_range",
            lambda: self.module.copy_rates_range(
                symbol, timeframe, date_from, date_to),
            ok_when=lambda r: r is not None,
        )

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def order_send(self, request: dict) -> Any:
        """Send a trade request. Raises :class:`MT5OrderRejectedError` when
        the terminal answers with a retcode != TRADE_RETCODE_DONE."""
        result = self._call("order_send",
                            lambda: self.module.order_send(request),
                            ok_when=lambda r: r is not None)
        retcode = getattr(result, "retcode", None)
        if retcode is None or retcode != TRADE_RETCODE_DONE:
            raise MT5OrderRejectedError(
                f"order_send rejected: retcode={retcode} "
                f"comment={getattr(result, 'comment', '')!r}",
                retcode=retcode,
                comment=str(getattr(result, "comment", "")),
            )
        return result

    def order_check(self, request: dict) -> Any:
        result = self._call("order_check",
                            lambda: self.module.order_check(request),
                            ok_when=lambda r: r is not None)
        retcode = getattr(result, "retcode", None)
        if retcode is None and isinstance(result, dict):
            retcode = result.get("retcode", 0)
        if retcode:
            comment = getattr(result, "comment", None)
            if comment is None and isinstance(result, dict):
                comment = result.get("comment", "")
            raise MT5CallError(
                f"order_check retcode={retcode} comment={comment!r}",
                retcode=retcode,
                comment=str(comment),
            )
        return result

    # ------------------------------------------------------------------
    # Market book (DOM) — used by realtime/book_feed.py
    # ------------------------------------------------------------------

    def market_book_add(self, symbol: str) -> bool:
        result = self._call("market_book_add",
                            lambda: self.module.market_book_add(symbol))
        return bool(result)

    def market_book_get(self, symbol: str) -> Any:
        return self._call("market_book_get",
                          lambda: self.module.market_book_get(symbol))

    def market_book_remove(self, symbol: str) -> bool:
        result = self._call("market_book_remove",
                            lambda: self.module.market_book_remove(symbol))
        return bool(result)