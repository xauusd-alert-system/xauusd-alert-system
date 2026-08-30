"""
Tests for Phase 9 Multi-Broker Execution Layer.
"""

import pytest

from execution.broker_adapter import (
    AccountSnapshot,
    MockFIXBrokerAdapter,
    MT5BrokerAdapter,
)
from mt5_adapter.testing import TRADE_RETCODE_DONE, TRADE_RETCODE_REJECT, MockMT5Module


@pytest.fixture()
def mock_mt5(monkeypatch):
    """Wire MT5BrokerAdapter to a MockMT5Module (no live terminal)."""
    mock = MockMT5Module()
    monkeypatch.setattr("mt5_adapter.lazy.get_mt5_module", lambda: mock)
    return mock


@pytest.fixture()
def adapter(mock_mt5):
    adapter = MT5BrokerAdapter()
    adapter.connect()
    return adapter


def test_mock_fix_broker_adapter_lifecycle():
    broker = MockFIXBrokerAdapter(initial_balance=50000.0)
    assert broker.connect() is True

    acc = broker.get_account_info()
    assert acc.balance == 50000.0
    assert acc.equity == 50000.0

    # Open position
    res = broker.open_market_order("XAUUSD", "buy", 1.0, sl=1990.0, tp=2020.0)
    assert res.success is True
    assert res.ticket is not None

    # Check position exists
    positions = broker.get_positions("XAUUSD")
    assert len(positions) == 1
    assert positions[0].ticket == res.ticket
    assert positions[0].direction == "buy"

    # Modify SL/TP
    mod_res = broker.modify_position(res.ticket, sl=1995.0, tp=2025.0)
    assert mod_res.success is True
    assert broker.positions[res.ticket].sl == 1995.0

    # Close position
    close_res = broker.close_position(res.ticket)
    assert close_res.success is True
    assert len(broker.get_positions()) == 0

    broker.disconnect()
    assert broker.connected is False


def test_mt5_broker_adapter_instantiation():
    adapter = MT5BrokerAdapter()
    assert adapter.connect() is True
    acc = adapter.get_account_info()
    assert isinstance(acc, AccountSnapshot)
    adapter.disconnect()


def test_mock_account_mode_and_symbol_constraints():
    netting = MockFIXBrokerAdapter(account_mode="netting")
    assert netting.get_account_mode() == "netting"
    hedging = MockFIXBrokerAdapter(account_mode="hedging")
    assert hedging.get_account_mode() == "hedging"
    constraints = netting.get_symbol_constraints("XAUUSD")
    assert constraints["available"] is True
    assert constraints["volume_step"] == 0.01
    assert constraints["volume_min"] == 0.01
    assert constraints["contract_size"] == 100.0
    assert constraints["account_margin_mode"] == "netting"
    assert constraints["tick_size"] == 0.01


def test_mt5_adapter_account_mode_and_constraints_shape():
    """The shim may not expose margin_mode; the contract must stay honest
    ('unknown' is a valid state — the executor gates on it)."""
    adapter = MT5BrokerAdapter()
    assert adapter.connect() is True
    mode = adapter.get_account_mode()
    assert mode in {"hedging", "netting", "unknown"}
    constraints = adapter.get_symbol_constraints("XAUUSD")
    for key in (
        "symbol_point",
        "tick_size",
        "digits",
        "trade_stops_level",
        "trade_freeze_level",
        "spread",
        "contract_size",
        "volume_min",
        "volume_max",
        "volume_step",
        "execution_mode",
        "account_margin_mode",
        "available",
    ):
        assert key in constraints, key
    adapter.disconnect()


# ---------------------------------------------------------------------------
# MT5BrokerAdapter against MockMT5Module (no live terminal)
# ---------------------------------------------------------------------------

XAU = "XAUUSD"


def _setup_market(mock):
    """Standard XAUUSD market snapshot: tick 2400.00/2400.30, real spec."""
    mock.set_symbol_info(
        XAU,
        digits=2,
        point=0.01,
        trade_stops_level=50,
        trade_tick_size=0.01,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_contract_size=100.0,
    )
    mock.set_tick(XAU, bid=2400.00, ask=2400.30)


class TestMT5AdapterOpenMarketOrder:
    def test_happy_path_buy(self, mock_mt5, adapter):
        _setup_market(mock_mt5)
        res = adapter.open_market_order(XAU, "buy", 0.10, sl=2390.0, tp=2420.0, comment="t1")
        assert res.success is True
        assert res.retcode == TRADE_RETCODE_DONE
        assert res.ticket is not None
        assert res.price == pytest.approx(2400.30)  # buy fills at ask
        # SL/TP must be forwarded to the broker request.
        req = mock_mt5.calls[[c[0] for c in mock_mt5.calls].index("order_send")][1][0]
        assert req["sl"] == 2390.0
        assert req["tp"] == 2420.0
        assert req["magic"] == 777111

    def test_sell_fills_at_bid(self, mock_mt5, adapter):
        _setup_market(mock_mt5)
        res = adapter.open_market_order(XAU, "sell", 0.10)
        assert res.success is True
        assert res.price == pytest.approx(2400.00)

    def test_no_tick_rejects_without_order_send(self, mock_mt5, adapter):
        mock_mt5.set_symbol_info(XAU, digits=2, point=0.01)
        res = adapter.open_market_order(XAU, "buy", 0.10)
        assert res.success is False
        assert "No tick data" in res.comment
        assert mock_mt5.call_count("order_send") == 0

    def test_order_send_reject_retcode(self, mock_mt5, adapter):
        _setup_market(mock_mt5)
        # Build a rejected result directly (retcode != DONE).
        from mt5_adapter.testing import _OrderResultTuple

        mock_mt5.order_send_handler = lambda request: _OrderResultTuple(
            retcode=TRADE_RETCODE_REJECT,
            deal=0,
            order=0,
            volume=request.get("volume", 0.0),
            price=request.get("price", 0.0),
            comment="no money",
            request_id=0,
            retcode_external=0,
        )
        res = adapter.open_market_order(XAU, "buy", 0.10)
        assert res.success is False
        assert res.retcode == TRADE_RETCODE_REJECT
        assert "no money" in res.comment

    def test_order_send_returns_none(self, mock_mt5, adapter):
        """Module failure (order_send -> None) must surface as retcode -1."""
        _setup_market(mock_mt5)
        mock_mt5.order_send_handler = lambda request: None
        res = adapter.open_market_order(XAU, "buy", 0.10)
        assert res.success is False
        assert res.retcode == -1


class TestMT5AdapterClosePosition:
    def test_happy_path_close(self, mock_mt5, adapter):
        _setup_market(mock_mt5)
        pos = mock_mt5.add_position(XAU, type=0, volume=0.10, price_open=2400.0)
        res = adapter.close_position(pos.ticket)
        assert res.success is True
        assert res.retcode == TRADE_RETCODE_DONE
        req = mock_mt5.calls[[c[0] for c in mock_mt5.calls].index("order_send")][1][0]
        assert req["position"] == pos.ticket
        assert req["type"] == mock_mt5.ORDER_TYPE_SELL  # closing a buy
        assert req["price"] == pytest.approx(2400.00)  # bid

    def test_close_partial_volume(self, mock_mt5, adapter):
        _setup_market(mock_mt5)
        pos = mock_mt5.add_position(XAU, type=0, volume=0.10)
        res = adapter.close_position(pos.ticket, volume=0.04)
        assert res.success is True
        req = mock_mt5.calls[[c[0] for c in mock_mt5.calls].index("order_send")][1][0]
        assert req["volume"] == pytest.approx(0.04)

    def test_close_missing_position(self, mock_mt5, adapter):
        res = adapter.close_position(999_999)
        assert res.success is False
        assert "not found" in res.comment
        assert mock_mt5.call_count("order_send") == 0

    def test_close_no_tick(self, mock_mt5, adapter):
        mock_mt5.add_position(XAU, type=0, volume=0.10)
        res = adapter.close_position(1_000_000)
        assert res.success is False
        assert "No tick" in res.comment

    def test_close_rejected(self, mock_mt5, adapter):
        from mt5_adapter.testing import _OrderResultTuple

        _setup_market(mock_mt5)
        pos = mock_mt5.add_position(XAU, type=0, volume=0.10)
        mock_mt5.order_send_handler = lambda request: _OrderResultTuple(
            retcode=TRADE_RETCODE_REJECT,
            deal=0,
            order=0,
            volume=0.0,
            price=0.0,
            comment="market closed",
            request_id=0,
            retcode_external=0,
        )
        res = adapter.close_position(pos.ticket)
        assert res.success is False
        assert res.retcode == TRADE_RETCODE_REJECT


class TestMT5AdapterModifyPosition:
    def test_happy_path_modify(self, mock_mt5, adapter):
        pos = mock_mt5.add_position(XAU, type=0, volume=0.10, sl=0.0, tp=0.0)
        res = adapter.modify_position(pos.ticket, sl=2390.0, tp=2420.0)
        assert res.success is True
        req = mock_mt5.calls[[c[0] for c in mock_mt5.calls].index("order_send")][1][0]
        assert req["action"] == mock_mt5.TRADE_ACTION_SLTP
        assert req["sl"] == 2390.0
        assert req["tp"] == 2420.0

    def test_modify_keeps_existing_sl_tp_when_none(self, mock_mt5, adapter):
        pos = mock_mt5.add_position(XAU, type=0, volume=0.10, sl=2390.0, tp=2420.0)
        res = adapter.modify_position(pos.ticket)
        assert res.success is True
        req = mock_mt5.calls[[c[0] for c in mock_mt5.calls].index("order_send")][1][0]
        assert req["sl"] == 2390.0
        assert req["tp"] == 2420.0

    def test_modify_missing_position(self, mock_mt5, adapter):
        res = adapter.modify_position(999_999, sl=1.0)
        assert res.success is False
        assert "not found" in res.comment


class TestMT5AdapterGetPositions:
    def test_maps_fields(self, mock_mt5, adapter):
        mock_mt5.add_position(XAU, type=0, volume=0.10, price_open=2400.0, magic=777111, sl=2390.0, tp=2420.0)
        positions = adapter.get_positions(XAU)
        assert len(positions) == 1
        p = positions[0]
        assert p.ticket == 1_000_000
        assert p.symbol == XAU
        assert p.direction == "buy"
        assert p.volume == pytest.approx(0.10)
        assert p.open_price == pytest.approx(2400.0)
        assert p.sl == pytest.approx(2390.0)
        assert p.tp == pytest.approx(2420.0)
        assert p.magic == 777111

    def test_sell_direction(self, mock_mt5, adapter):
        mock_mt5.add_position(XAU, type=1, volume=0.10)
        assert adapter.get_positions()[0].direction == "sell"

    def test_empty_positions(self, mock_mt5, adapter):
        assert adapter.get_positions() == []


class TestMT5AdapterAccountMode:
    def test_hedging(self, monkeypatch):
        mock = MockMT5Module(margin_mode=mock_mt5_hedging())
        monkeypatch.setattr("mt5_adapter.lazy.get_mt5_module", lambda: mock)
        adapter = MT5BrokerAdapter()
        adapter.connect()
        assert adapter.get_account_mode() == "hedging"

    def test_netting(self, monkeypatch):
        from mt5_adapter.testing import ACCOUNT_MARGIN_MODE_RETAIL_NETTING

        mock = MockMT5Module(margin_mode=ACCOUNT_MARGIN_MODE_RETAIL_NETTING)
        monkeypatch.setattr("mt5_adapter.lazy.get_mt5_module", lambda: mock)
        adapter = MT5BrokerAdapter()
        adapter.connect()
        assert adapter.get_account_mode() == "netting"

    def test_unknown_mode(self, mock_mt5, adapter):
        mock_mt5.account = mock_mt5.account._replace(margin_mode=999)
        assert adapter.get_account_mode() == "unknown"

    def test_account_info_none(self, mock_mt5, adapter):
        mock_mt5._initialized = False  # account_info() -> None
        assert adapter.get_account_mode() == "unknown"


class TestMT5AdapterSymbolConstraints:
    def test_full_snapshot(self, mock_mt5, adapter):
        _setup_market(mock_mt5)
        c = adapter.get_symbol_constraints(XAU)
        assert c["available"] is True
        assert c["symbol"] == XAU
        assert c["digits"] == 2
        assert c["symbol_point"] == 0.01
        assert c["tick_size"] == 0.01
        assert c["trade_stops_level"] == 50
        assert c["trade_freeze_level"] == 0
        assert c["volume_min"] == 0.01
        assert c["volume_max"] == 100.0
        assert c["volume_step"] == 0.01
        assert c["contract_size"] == 100.0
        assert c["spread"] == pytest.approx(0.30)

    def test_zero_spread_when_no_tick(self, mock_mt5, adapter):
        mock_mt5.set_symbol_info(XAU, digits=2, point=0.01)
        c = adapter.get_symbol_constraints(XAU)
        assert c["available"] is True
        assert c["spread"] == 0.0

    def test_symbol_info_none_is_graceful(self, mock_mt5, adapter):
        c = adapter.get_symbol_constraints("UNKNOWN")
        assert c["available"] is False
        assert c["reason"] == "symbol_info unavailable"
        assert c["account_margin_mode"] == adapter.get_account_mode()
        assert c["volume_min"] == 0.0

    def test_fallback_tick_size_from_point(self, mock_mt5, adapter):
        """trade_tick_size=0 -> falls back to point."""
        mock_mt5.set_symbol_info(XAU, digits=2, point=0.01, trade_tick_size=0.0)
        c = adapter.get_symbol_constraints(XAU)
        assert c["tick_size"] == 0.01


def mock_mt5_hedging():
    from mt5_adapter.testing import ACCOUNT_MARGIN_MODE_RETAIL_HEDGING

    return ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
