"""
Tests for Phase 9 Multi-Broker Execution Layer.
"""

from execution.broker_adapter import (
    AccountSnapshot,
    MockFIXBrokerAdapter,
    MT5BrokerAdapter,
)


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
