"""Tests for paper trading replay batch and SessionSimulator (usstocks/paper.py)."""
import os
import tempfile
from datetime import datetime, timezone
import pytest

from usstocks.data.replay_provider import dump_bars
from usstocks.journal import UsJournal
from usstocks.models import Bar
from usstocks.paper import SessionSimulator, run_batch, _write_summary_csv, main


def _create_mock_session_bars(symbol: str, session_date: str):
    """Generate minimal 1m bars for a symbol."""
    tz = timezone.utc
    base_dt = datetime.fromisoformat(f"{session_date}T13:30:00+00:00")
    bars = []
    # 20 bars
    for i in range(25):
        t = datetime.fromtimestamp(base_dt.timestamp() + i * 60, tz=tz)
        p = 100.0 + (i * 0.5 if i < 10 else 5.0 - (i - 10) * 0.2)
        bars.append(Bar(ts=t, open=p, high=p + 0.5, low=p - 0.5, close=p + 0.1, volume=5000.0))
    return bars


def test_session_simulator_empty_and_run():
    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "paper.sqlite")
        journal = UsJournal(db_path)
        cfg = {
            "risk": {
                "risk_per_trade_usd": 10.0,
                "personal_daily_stop_usd": -20.0,
                "max_trades_per_day": 2,
                "max_consecutive_losses": 2,
                "daily_profit_lock_usd": 20.0,
                "no_new_entries_minutes_before_close": 25,
            },
            "challenge": {"max_notional_usd": 5000.0},
            "scanner": {"poll_seconds": 60},
            "session": {"holidays": []},
            "us_stocks": {"tech_symbols": ["NVDA", "AAPL"]},
        }
        symbol_ids = {"NVDA": "1", "AAPL": "2", "QQQ": "3", "SPY": "4"}
        sim = SessionSimulator(cfg, journal, symbol_ids, ["NVDA", "AAPL"], "2026-08-27")
        
        bars = {
            "NVDA": _create_mock_session_bars("NVDA", "2026-08-27"),
            "AAPL": _create_mock_session_bars("AAPL", "2026-08-27"),
            "QQQ": _create_mock_session_bars("QQQ", "2026-08-27"),
            "SPY": _create_mock_session_bars("SPY", "2026-08-27"),
        }
        stats = sim.run(bars)
        assert stats["session"] == "2026-08-27"
        assert "trades" in stats
        assert "win_rate" in stats
        assert "avg_r" in stats
        journal.close()


def test_run_batch_and_write_summary(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        csv_root = os.path.join(td, "csv_root")
        export_dir = os.path.join(td, "export")
        os.makedirs(csv_root, exist_ok=True)
        os.makedirs(export_dir, exist_ok=True)
        
        session_date = "2026-08-27"
        symbols = ["AAPL", "QQQ", "SPY"]
        for sym in symbols:
            bars = _create_mock_session_bars(sym, session_date)
            p = os.path.join(csv_root, f"{sym}_{session_date}.csv")
            dump_bars(bars, p)

        db_path = os.path.join(td, "paper_batch.sqlite")
        summary = run_batch(
            csv_root=csv_root,
            session_dates=[session_date, "2026-08-28"],  # second date has missing files to test branch
            universe=["AAPL"],
            cfg={},
            journal_path=db_path,
        )
        assert len(summary) >= 1
        summary_csv = os.path.join(export_dir, "summary.csv")
        _write_summary_csv(summary, summary_csv)
        assert os.path.exists(summary_csv)
        _write_summary_csv([], os.path.join(export_dir, "empty.csv"))


def test_paper_main_cli(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        csv_root = os.path.join(td, "csv")
        os.makedirs(csv_root, exist_ok=True)
        session_date = "2026-08-27"
        for sym in ["NVDA", "QQQ", "SPY"]:
            bars = _create_mock_session_bars(sym, session_date)
            p = os.path.join(csv_root, f"{sym}_{session_date}.csv")
            dump_bars(bars, p)

        db_path = os.path.join(td, "paper.sqlite")
        summary_csv = os.path.join(td, "sum.csv")
        monkeypatch.setattr(
            "sys.argv",
            [
                "paper.py",
                "--csv-root", csv_root,
                "--dates", session_date,
                "--universe", "NVDA",
                "--journal", db_path,
                "--summary-csv", summary_csv,
            ]
        )
        main()
        assert os.path.exists(summary_csv)
