"""Unit tests for MT5Client error wrapping, counting and DI (ТЗ 8.6)."""

from __future__ import annotations

import pytest

from mt5_adapter.client import TRADE_RETCODE_DONE, MT5Client
from mt5_adapter.errors import (
    MT5CallError,
    MT5NotInitializedError,
    MT5OrderRejectedError,
    MT5TimeoutError,
)
from mt5_adapter.rate_limiter import MT5RateLimiter
from mt5_adapter.testing import MockMT5Module


@pytest.fixture()
def mock():
    m = MockMT5Module()
    m.set_symbol_info("XAUUSD", digits=2, point=0.01)
    m.set_tick("XAUUSD", bid=2400.10, ask=2400.40)
    return m


@pytest.fixture()
def client(mock):
    c = MT5Client(mt5_module=mock)
    c.initialize()
    return c


# ---------------------------------------------------------------------
# Error wrapping
# ---------------------------------------------------------------------


def test_client_wraps_errors_none_result(mock):
    """A None return from the low-level module raises MT5NotInitializedError."""
    client = MT5Client(mt5_module=mock)
    mock.ticks.clear()  # symbol_info_tick would return None
    with pytest.raises(MT5NotInitializedError):
        client.symbol_info_tick("XAUUSD")


def test_client_wraps_errors_initialize_false(mock):
    client = MT5Client(mt5_module=mock)
    mock.initialize = lambda *a, **k: False
    mock.set_last_error(-6, "terminal not found")
    with pytest.raises(MT5CallError) as exc:
        client.initialize()
    assert "terminal not found" in str(exc.value)


def test_client_wraps_errors_account_info_none(mock):
    client = MT5Client(mt5_module=mock)
    client.initialize()
    mock.account = None
    with pytest.raises(MT5NotInitializedError):
        client.account_info()


def test_client_wraps_errors_rates_empty(mock):
    import numpy as np

    client = MT5Client(mt5_module=mock)
    client.initialize()
    mock.rates["XAUUSD"] = np.array([], dtype=[("time", "<i8")])
    with pytest.raises(MT5CallError):
        client.copy_rates_from_pos("XAUUSD", 5, 1, 10)


# ---------------------------------------------------------------------
# Call counting
# ---------------------------------------------------------------------


def test_client_counts_calls(client):
    # fixture already called initialize() once
    client.symbol_info_tick("XAUUSD")
    client.symbol_info_tick("XAUUSD")
    client.symbol_info("XAUUSD")
    client.positions_get()
    assert client.calls["symbol_info_tick"] == 2
    assert client.calls["symbol_info"] == 1
    assert client.calls_total == 5  # 4 reads + initialize
    # all four read methods are poll methods (initialize is not)
    assert client.calls_per_poll == 4
    client.reset_poll_counters()
    assert client.calls_per_poll == 0
    assert client.calls_total == 5


def test_client_counts_initialize_and_shutdown(mock):
    client = MT5Client(mt5_module=mock)
    client.initialize()
    client.shutdown()
    assert client.calls["initialize"] == 1
    assert client.calls["shutdown"] == 1
    # lifecycle calls are not poll calls
    assert client.calls_per_poll == 0


# ---------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------


def test_client_symbol_info_tick_cached(client, mock):
    a = client.symbol_info_tick_cached("XAUUSD")
    b = client.symbol_info_tick_cached("XAUUSD")
    assert a is b
    assert mock.call_count("symbol_info_tick") == 1
    assert client.calls["symbol_info_tick"] == 1

    mock.set_tick("XAUUSD", bid=2401.0, ask=2401.3)
    fresh = client.symbol_info_tick("XAUUSD")
    assert fresh.bid == 2401.0
    assert mock.call_count("symbol_info_tick") == 2


def test_client_symbol_info_cached(mock):
    client = MT5Client(mt5_module=mock)
    client.initialize()
    client.symbol_info_cached("XAUUSD")
    client.symbol_info_cached("XAUUSD")
    assert mock.call_count("symbol_info") == 1


# ---------------------------------------------------------------------
# Trading wrappers
# ---------------------------------------------------------------------


def test_client_order_send_rejects_bad_retcode(mock):
    from mt5_adapter.testing import TRADE_RETCODE_REJECT

    client = MT5Client(mt5_module=mock)
    client.initialize()
    mock.order_send_handler = lambda req: type("R", (), {"retcode": TRADE_RETCODE_REJECT, "comment": "no money"})()
    with pytest.raises(MT5OrderRejectedError) as exc:
        client.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.1})
    assert exc.value.retcode == TRADE_RETCODE_REJECT
    assert exc.value.comment is not None
    assert "no money" in exc.value.comment


def test_client_order_send_ok(client):
    res = client.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.1, "price": 2400.4})
    assert res.retcode == TRADE_RETCODE_DONE


def test_client_order_check_bad_retcode(mock):
    client = MT5Client(mt5_module=mock)
    client.initialize()
    mock.order_check = lambda req: {"retcode": 10019, "comment": "bad volume"}
    with pytest.raises(MT5CallError):
        client.order_check({"symbol": "XAUUSD"})


# ---------------------------------------------------------------------
# Constants passthrough / timeout
# ---------------------------------------------------------------------


def test_client_exposes_constants(mock):
    from mt5_adapter import testing as t

    client = MT5Client(mt5_module=mock)
    assert client.TIMEFRAME_M5 == t.TIMEFRAME_M5
    assert client.TRADE_ACTION_DEAL == t.TRADE_ACTION_DEAL
    with pytest.raises(AttributeError):
        client.NO_SUCH_CONSTANT


def test_client_timeout_raises():
    class SlowMock(MockMT5Module):
        def symbol_info_tick(self, symbol):
            import time as _time

            _time.sleep(0.05)
            return super().symbol_info_tick(symbol)

    mock = SlowMock()
    mock.set_tick("XAUUSD", bid=1.0, ask=1.1)
    client = MT5Client(mt5_module=mock, timeout_s=0.01)
    with pytest.raises(MT5TimeoutError):
        client.symbol_info_tick("XAUUSD")


def test_client_lazy_import_not_triggered_when_module_given():
    """With an injected module the client must not import MetaTrader5."""
    client = MT5Client(mt5_module=MockMT5Module())
    assert client._mt5_loaded is True
    # accessing module does not invoke the real import path
    assert client.module is not None


def test_client_disabled_rate_limiter_by_default(mock):
    client = MT5Client(mt5_module=mock)
    assert not client.rate_limiter.enabled


def test_client_rate_limiter_opt_in(mock):
    limiter = MT5RateLimiter(max_calls_per_second=10)
    client = MT5Client(mt5_module=mock, rate_limiter=limiter)
    client.initialize()
    client.symbol_info_tick("XAUUSD")
    assert limiter.enabled
