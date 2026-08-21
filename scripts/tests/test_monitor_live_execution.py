"""
Tests for scripts/monitor_live_execution.py.

Uses a temporary SQLite database populated through data.trade_logger so the
test exercises the same schema and read path as production, without MT5.
"""
import glob
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data.trade_logger import init_trade_log_schema, log_trade_entry, log_trade_close
from scripts.monitor_live_execution import compute_live_metrics


def test_compute_live_metrics_basic():
    df = pd.DataFrame({
        "ticket": [1, 2, 3, 4, 5],
        "symbol": ["BTCUSD"] * 5,
        "bias": ["long", "short", "long", "short", "long"],
        "entry_time": [1000, 2000, 3000, 4000, 5000],
        "entry_price": [50000.0] * 5,
        "close_time": [1100, 2100, 3100, 4100, 5100],
        "close_price": [50100.0, 49900.0, 50070.0, 49950.0, 50040.0],
        "pnl": [10.0, -5.0, 7.0, -3.0, 4.0],
        "outcome": [1, 0, 1, 0, 1],
        "features": ["{}"] * 5,
    })
    m = compute_live_metrics(df)
    assert m["n_trades"] == 5
    assert m["total_pnl"] == 13.0
    assert m["win_rate"] == 60.0
    # PF = (10+7+4) / (5+3) = 21 / 8 = 2.625
    assert m["profit_factor"] == pytest.approx(2.625, abs=0.01)
    assert m["median_duration_min"] == pytest.approx(1.7, abs=0.01)


def test_compute_live_metrics_empty():
    m = compute_live_metrics(pd.DataFrame())
    assert m["n_trades"] == 0
    assert m["total_pnl"] == 0.0
    assert m["profit_factor"] == 0.0


def test_monitor_cli_writes_csv(tmp_path, monkeypatch):
    """End-to-end CLI smoke: create a temporary executed_trades DB, run the
    monitor, and confirm the daily summary CSV is produced."""
    db = tmp_path / "test_exec.sqlite"
    init_trade_log_schema(str(db))

    log_trade_entry(str(db), 100, "BTCUSD", "long", 1_700_000_000, 50000.0,
                    {"p_long": 0.8, "p_short": 0.2})
    log_trade_close(str(db), 100, 1_700_000_100, 50100.0, 25.0)

    monkeypatch.chdir(tmp_path)
    from scripts.monitor_live_execution import main

    out_dir = str(tmp_path / "logs")
    main(["--asset", "BTCUSD", "--db-path", str(db), "--out-dir", out_dir])

    csvs = glob.glob(os.path.join(out_dir, "live_execution_btcusd_*.csv"))
    assert len(csvs) == 1
    df = pd.read_csv(csvs[0])
    assert "close_date" in df.columns
    assert df.iloc[0]["n_trades"] == 1
    assert df.iloc[0]["total_pnl"] == 25.0