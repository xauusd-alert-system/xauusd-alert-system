"""Tests for Journal SQLite schema and indexes (P0-5)."""
import sqlite3
import tempfile
from datetime import datetime, timezone
from usstocks.journal import UsJournal
from usstocks.models import PremarketSnapshot, RiskEvent, TradeSignal


def test_journal_indexes_created_and_used():
    with tempfile.TemporaryDirectory() as td:
        db_path = f"{td}/test_journal.sqlite"
        journal = UsJournal(db_path)

        # Inspect indexes created
        indexes = {
            r[0]
            for r in journal._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        expected = {
            "idx_signals_date",
            "idx_signals_decision_created",
            "idx_signals_symbol_decision",
            "idx_outcomes_signal",
            "idx_outcomes_recorded",
            "idx_watchlist_date_symbol",
            "idx_risk_events_date_ts",
        }
        assert expected <= indexes

        # Verify query plan uses index for latest_signal
        plan = journal._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM us_signals WHERE decision='pending' ORDER BY created_at DESC LIMIT 1"
        ).fetchall()
        plan_str = " ".join(" ".join(str(val) for val in tuple(p)) for p in plan)
        assert "idx_signals" in plan_str or "USING INDEX" in plan_str or "SEARCH" in plan_str

        # Test inserting and querying data
        journal.ensure_session("2026-08-27")
        snap = PremarketSnapshot(
            symbol="AAPL",
            price=150.0,
            prev_close=148.0,
            gap_pct=1.35,
            relative_volume=2.5,
            avg_daily_dollar_volume=50_000_000,
            spread_pct=0.02,
        )
        journal.save_watchlist("2026-08-27", [snap], in_watchlist={"AAPL"})

        sig = TradeSignal(
            symbol="AAPL",
            side="long",
            entry_low=150.0,
            entry_high=150.2,
            stop=149.5,
            tp1=150.9,
            tp2=151.6,
            risk_per_share=0.7,
            shares=14,
            notional_usd=2102.8,
            planned_risk_usd=9.8,
            grade="A",
            created_at=datetime.now(timezone.utc),
        )
        journal.save_signal(sig, "2026-08-27")
        latest = journal.latest_signal(symbol="AAPL", decision="pending")
        assert latest is not None
        assert latest["symbol"] == "AAPL"

        # Record outcome
        journal.record_outcome(sig.signal_id, pnl_usd=15.0, planned_risk_usd=9.8, confirmed_by="12345")
        assert journal.day_pnl("2026-08-27") == 15.0

        journal.close()
