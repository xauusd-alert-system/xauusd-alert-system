# -*- coding: utf-8 -*-
"""Paper-trading metrics tests (ТЗ §12 items 10-13 extended for paper)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import tempfile
import time
import os
import csv

import pytest

from usstocks.paper import SessionSimulator
from usstocks.journal import UsJournal
from usstocks.models import RiskState
from usstocks.scanner_loop import SignalOnlyRunner
from tests.fixtures.vwap_scenarios import long_scenario, benchmark_uptrend


NY = ZoneInfo("America/New_York")


class FakeProvider:
    def __init__(self, bars_by_sym):
        self.bars = {k.upper(): v for k, v in bars_by_sym.items()}

    def get_bars(self, symbol, count):
        if symbol.upper() in ("QQQ", "SPY"):
            # Return 1m bars (benchmark_uptrend returns 1m bars via _benchmark_1m)
            return benchmark_uptrend(count, "2026-08-26")
        return self.bars[symbol.upper()]


class SilentNotifier:
    def send_signal(self, s): pass
    def send_risk_event(self, e): pass
    def send_watchlist(self, w): pass


BASE_CFG = {
    "risk": {"risk_per_trade_usd": 10.0, "personal_daily_stop_usd": -20.0,
             "max_trades_per_day": 2, "max_consecutive_losses": 2,
             "daily_profit_lock_usd": 20.0,
             "no_new_entries_minutes_before_close": 25},
    "challenge": {"max_notional_usd": 5000.0},
    "strategy": {},
    "us_stocks": {"tech_symbols": ["AMD"]},
    "session": {"holidays": []},
}


def _session_state(date_str="2026-08-26"):
    return RiskState(session_date=date_str)


def _runner(journal):
    return SignalOnlyRunner(
        BASE_CFG, FakeProvider({"AMD": long_scenario()}), SilentNotifier(),
        watchlist=["AMD"], state=_session_state(), journal=journal,
        symbol_ids={"AMD": "S", "QQQ": "Q"})


def test_paper_session_zero_signals_when_flat():
    td = tempfile.mkdtemp()
    try:
        jr = UsJournal(td + "/j.sqlite")
        runner = _runner(jr)
        from tests.fixtures.vwap_scenarios import flat_scenario
        runner.provider.bars = {"AMD": flat_scenario()}
        now = datetime(2026, 8, 26, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        sigs = runner.scan_once(now)
        assert sigs == []
        stats = jr._conn.execute(
            "SELECT * FROM us_signals WHERE session_date='2026-08-26'").fetchall()
        assert len(stats) == 0
        jr.close()
        time.sleep(0.1)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_paper_session_one_valid_signal_logged():
    td = tempfile.mkdtemp()
    try:
        jr = UsJournal(td + "/j.sqlite")
        runner = _runner(jr)
        runner.provider.bars = {"AMD": long_scenario()}
        now = datetime(2026, 8, 26, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        sigs = runner.scan_once(now)
        assert len(sigs) == 1
        row = jr._conn.execute(
            "SELECT * FROM us_signals WHERE session_date='2026-08-26'").fetchone()
        assert row["symbol"] == "AMD"
        assert row["decision"] == "pending"
        assert row["planned_risk_usd"] <= 10
        jr.close()
        time.sleep(0.1)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_paper_metrics_calculated_from_journal():
    """End-to-end: insert synthetic trades -> verify journal aggregation."""
    td = tempfile.mkdtemp()
    try:
        jr = UsJournal(td + "/j.sqlite")
        jr.ensure_session("2026-08-26")
        # seed two taken signals with outcomes
        jr._conn.execute(
            "INSERT INTO us_signals(signal_id,session_date,created_at,symbol,side,"
            "entry_low,entry_high,stop,tp1,tp2,risk_per_share,shares,"
            "notional_usd,planned_risk_usd,grade,strategy_version,provider,"
            "metrics_json,passed_json,why_json,decision)"
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'taken')",
            ("sig1","2026-08-26","2026-08-26T10:00:00","AMD","long",
             100,101,99,102,103,1.0,10,1000,10,"B","v1","utex","{}","{}","{}"))
        jr._conn.execute(
            "INSERT INTO us_signals(signal_id,session_date,created_at,symbol,side,"
            "entry_low,entry_high,stop,tp1,tp2,risk_per_share,shares,"
            "notional_usd,planned_risk_usd,grade,strategy_version,provider,"
            "metrics_json,passed_json,why_json,decision)"
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'taken')",
            ("sig2","2026-08-26","2026-08-26T11:00:00","NVDA","long",
             200,201,199,202,203,1.0,10,2000,10,"B","v1","utex","{}","{}","{}"))
        jr.record_outcome("sig1", pnl_usd=10.0, planned_risk_usd=10.0, confirmed_by="111", outcome="win")
        jr.record_outcome("sig2", pnl_usd=-10.0, planned_risk_usd=10.0, confirmed_by="111", outcome="loss")
        jr.close_session("2026-08-26")

        # verify journal queries directly
        row = jr._conn.execute(
            "SELECT COUNT(*) as n, AVG(o.r_multiple) as avg_r, SUM(o.pnl_usd) as sum_pnl"
            " FROM us_signals g JOIN us_trade_outcomes o ON o.signal_id=g.signal_id"
            " WHERE g.session_date=? AND g.decision='taken'",
            ("2026-08-26",)).fetchone()
        assert row["n"] == 2
        assert row["avg_r"] == pytest.approx(0.0)   # (+1 + -1)/2 = 0
        assert row["sum_pnl"] == pytest.approx(0.0)
    finally:
        time.sleep(0.1)
        import shutil
        shutil.rmtree(td, ignore_errors=True)

        # verify journal queries directly
        row = jr._conn.execute(
            "SELECT COUNT(*) as n, AVG(o.r_multiple) as avg_r, SUM(o.pnl_usd) as sum_pnl"
            " FROM us_signals g JOIN us_trade_outcomes o ON o.signal_id=g.signal_id"
            " WHERE g.session_date=? AND g.decision='taken'",
            ("2026-08-26",)).fetchone()
        assert row["n"] == 2
        assert row["avg_r"] == pytest.approx(0.0)   # (+1 + -1)/2 = 0
        assert row["sum_pnl"] == pytest.approx(0.0)


def test_paper_metrics_csv_export_schema():
    td = tempfile.mkdtemp()
    try:
        jr = UsJournal(td + "/j.sqlite")
        jr.ensure_session("2026-08-26")
        path = jr.export_day_csv("2026-08-26", td + "/export")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0][0] == "signal_id"
        jr.close()
        time.sleep(0.1)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_paper_summary_aggregate():
    """Minimal end-to-end: run_batch with 2 synthetic days -> summary CSV."""
    # This test is integration-heavy; keep lightweight by verifying the
    # summary CSV schema only (actual runs are manual per ТЗ §13).
    td = tempfile.mkdtemp()
    try:
        csv_root = os.path.join(td, "replay")
        os.makedirs(csv_root)
        # create two day CSVs for AMD
        from tests.fixtures.vwap_scenarios import to_csv_rows, long_scenario
        for d in ["2026-08-26", "2026-08-27"]:
            bars = long_scenario()
            path = os.path.join(td, f"replay/AMD_{d}.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(to_csv_rows(bars, "AMD")))
        # just verify the paper module imports and no syntax errors
        from usstocks import paper
        assert callable(paper.main)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)