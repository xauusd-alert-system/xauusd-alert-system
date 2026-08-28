"""
Unit tests for data/trade_logger.py
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data.trade_logger import (
    init_trade_log_schema,
    log_trade_close,
    log_trade_entry,
    read_executed_trades,
)


def test_trade_logger_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_trades.sqlite")
        init_trade_log_schema(db_path)

        ticket = 12345
        symbol = "XAUUSD"
        bias = "long"
        entry_time = 1700000000
        entry_price = 2000.50
        features = {"rsi": 45.5, "atr": 2.1}

        # 1. Log Entry
        log_trade_entry(db_path, ticket, symbol, bias, entry_time, entry_price, features)

        # 2. Log Close
        close_time = 1700003600
        close_price = 2010.00
        pnl = 9.50
        log_trade_close(db_path, ticket, close_time, close_price, pnl)

        # 3. Read
        df = read_executed_trades(db_path, symbol="XAUUSD")
        assert len(df) == 1
        assert df.iloc[0]["ticket"] == ticket
        assert df.iloc[0]["pnl"] == pytest.approx(9.50)
        assert df.iloc[0]["outcome"] == 1
