"""
Tests for Phase 9 Multi-Broker Execution Layer.
"""
import pytest
from execution.broker_adapter import (
    MT5BrokerAdapter,
    MockFIXBrokerAdapter,
    AccountSnapshot,
    PositionSnapshot,
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
