"""Round-trip tests for MockMT5Module and the typed mirrors (ТЗ 8.6)."""
from __future__ import annotations

import pytest

from mt5_adapter.client import MT5Client
from mt5_adapter.testing import MockMT5Module, TRADE_RETCODE_DONE
from mt5_adapter.types import (
    AccountInfo,
    DealInfo,
    OrderResult,
    PositionInfo,
    SymbolInfo,
    Tick,
)


def test_mock_module_roundtrip():
    """initialize -> reads -> order_send -> close: full adapter flow on the mock."""
    mock = MockMT5Module(balance=5000.0, equity=5100.0)
    mock.set_symbol_info("XAUUSD", digits=2, point=0.01)
    mock.set_tick("XAUUSD", bid=2400.10, ask=2400.40)

    client = MT5Client(mt5_module=mock)
    assert client.initialize() is True
    assert client.terminal_info()["connected"] is True

    acc = client.account_info()
    assert acc.balance == 5000.0 and acc.equity == 5100.0

    info = client.symbol_info("XAUUSD")
    assert info.digits == 2 and info.point == 0.01

    tick = client.symbol_info_tick("XAUUSD")
    assert tick.bid == 2400.10 and tick.ask == 2400.40

    pos = mock.add_position("XAUUSD", type=0, volume=0.1, magic=777111)
    positions = client.positions_get(magic=777111)
    assert list(positions) == [pos]

    res = client.order_send({"action": 1, "symbol": "XAUUSD",
                             "volume": 0.1, "price": 2400.40})
    assert res.retcode == TRADE_RETCODE_DONE

    client.shutdown()
    assert mock._initialized is False
    assert mock.call_count("symbol_info_tick") == 1


def test_mock_module_tracks_calls():
    mock = MockMT5Module()
    mock.set_tick("XAUUSD", bid=1.0, ask=1.1)
    mock.set_tick("BTCUSD", bid=60000.0, ask=60001.0)
    client = MT5Client(mt5_module=mock)
    client.initialize()
    client.symbol_info_tick("XAUUSD")
    client.symbol_info_tick("BTCUSD")
    assert mock.call_count("symbol_info_tick") == 2
    names = [c[0] for c in mock.calls]
    assert names == ["initialize", "symbol_info_tick", "symbol_info_tick"]


def test_mock_module_uninitialized_returns_none():
    mock = MockMT5Module()
    client = MT5Client(mt5_module=mock)
    with pytest.raises(Exception):
        client.account_info()  # None before initialize -> adapter error


def test_mock_market_book():
    mock = MockMT5Module()
    mock.book["XAUUSD"] = [{"price": 2400.0, "volume": 5, "type": 1}]
    client = MT5Client(mt5_module=mock)
    client.initialize()
    assert client.market_book_add("XAUUSD") is True
    book = client.market_book_get("XAUUSD")
    assert book and book[0]["price"] == 2400.0
    assert client.market_book_remove("XAUUSD") is True


# ---------------------------------------------------------------------
# Typed mirrors
# ---------------------------------------------------------------------

def test_types_roundtrip_tick():
    mock = MockMT5Module()
    mock.set_tick("XAUUSD", bid=2400.10, ask=2400.40, time=1700000001)
    raw = mock.symbol_info_tick("XAUUSD")
    typed = Tick.from_raw(raw)
    assert typed.bid == 2400.10
    assert typed.time == 1700000001
    with pytest.raises(ValueError):
        Tick.from_raw(None)


def test_types_roundtrip_symbol_and_account():
    mock = MockMT5Module()
    mock.set_symbol_info("XAUUSD", digits=2, point=0.01)
    info = SymbolInfo.from_raw(mock.symbol_info("XAUUSD"))
    assert info.name == "XAUUSD" and info.digits == 2
    acc = AccountInfo.from_raw(mock.account)
    assert acc.currency == "USD" and acc.margin_mode == 2
    with pytest.raises(ValueError):
        SymbolInfo.from_raw(None)


def test_types_roundtrip_position_deal_order():
    mock = MockMT5Module()
    pos = mock.add_position("XAUUSD", ticket=42, type=1, volume=0.2,
                            price_open=2401.0, magic=777111, profit=-1.5)
    typed_pos = PositionInfo.from_raw(pos)
    assert typed_pos.ticket == 42 and typed_pos.magic == 777111

    deal = DealInfo.from_raw(type("D", (), {
        "ticket": 7, "order": 8, "position_id": 42, "symbol": "XAUUSD",
        "type": 0, "entry": 0, "volume": 0.2, "price": 2401.0,
        "profit": 0.0, "commission": -0.2, "swap": 0.0, "magic": 777111,
        "comment": "", "time": 1700000000})())
    assert deal.position_id == 42 and deal.commission == -0.2

    order = OrderResult.from_raw(type("R", (), {
        "retcode": 10009, "deal": 1, "order": 2, "volume": 0.2,
        "price": 2401.0, "comment": "", "request_id": 3,
        "retcode_external": 0})())
    assert order.ok is True
    bad = OrderResult(retcode=10004)
    assert bad.ok is False

    for factory, raw in ((PositionInfo, None), (DealInfo, None),
                         (OrderResult, None)):
        with pytest.raises(ValueError):
            factory.from_raw(raw)
