"""
Tests for the paper-trade accumulator DB helpers.

These tests only exercise the paper_trades persistence layer; they do not
require MT5, a model, or market data.
"""

import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.paper_accumulate_wide_filtered import (
    clear_paper_trades,
    init_paper_db,
    save_paper_trades,
)


def make_fake_trade(entry_ts=1_700_000_000, exit_ts=1_700_001_000, direction=1, pnl=10.0):
    return SimpleNamespace(
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_price=50000.0,
        exit_price=50010.0,
        direction=direction,
        pnl=pnl,
        exit_reason="tp1",
    )


@pytest.fixture
def paper_db(tmp_path):
    db = str(tmp_path / "paper.sqlite")
    init_paper_db(db)
    return db


def test_init_creates_table(paper_db):
    conn = sqlite3.connect(paper_db)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_trades'")
    assert cur.fetchone() == ("paper_trades",)
    conn.close()


def test_save_and_clear(paper_db):
    trades = [make_fake_trade(), make_fake_trade(direction=-1, pnl=-5.0)]
    save_paper_trades(paper_db, "XAUUSD", "wide_trend_filtered", trades)

    conn = sqlite3.connect(paper_db)
    count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    conn.close()
    assert count == 2

    clear_paper_trades(paper_db, "XAUUSD", "wide_trend_filtered")
    conn = sqlite3.connect(paper_db)
    count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    conn.close()
    assert count == 0


def test_save_skips_unclosed_trades(paper_db):
    # A trade with exit_ts=None must not be persisted.
    open_trade = SimpleNamespace(
        entry_ts=1_700_000_000,
        exit_ts=None,
        entry_price=1.0,
        exit_price=None,
        direction=1,
        pnl=None,
        exit_reason=None,
    )
    save_paper_trades(paper_db, "XAUUSD", "wide_trend_filtered", [open_trade])

    conn = sqlite3.connect(paper_db)
    count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    conn.close()
    assert count == 0
