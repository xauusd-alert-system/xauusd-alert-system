"""Adapter-level error types for the MT5 adapter layer (ТЗ 8.6).

The rest of the codebase must never import MetaTrader5 directly; instead it
works with :class:`mt5_adapter.client.MT5Client`, which translates raw MT5
failures (``None`` returns, non-zero retcodes, terminal unavailability) into
the typed exceptions below.
"""

from __future__ import annotations


class MT5AdapterError(Exception):
    """Base class for all MT5 adapter errors."""


class MT5NotInitializedError(MT5AdapterError):
    """The MT5 terminal connection is not initialized / was lost."""


class MT5CallError(MT5AdapterError):
    """A single MT5 API call failed.

    Attributes:
        retcode: the raw MT5 return code (0 = OK convention of the adapter;
            for trade results the terminal's ``TRADE_RETCODE_*`` is passed
            through unchanged).
        comment: human-readable failure comment (from ``last_error()`` or the
            trade-result ``comment`` field).
    """

    def __init__(self, message: str = "MT5 call failed", retcode: int | None = None, comment: str | None = None):
        super().__init__(message)
        self.retcode = retcode
        self.comment = comment


class MT5TimeoutError(MT5AdapterError):
    """An MT5 call exceeded the configured timeout budget."""


class MT5RateLimitedError(MT5AdapterError):
    """The caller refused to wait for a rate-limiter slot (strict mode)."""


class MT5OrderRejectedError(MT5CallError):
    """``order_send`` was rejected by the terminal (retcode != DONE)."""
