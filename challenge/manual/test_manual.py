# -*- coding: utf-8 -*-
"""Unit tests for the manual system (ТЗ). Run: python -m unittest challenge.manual.test_manual"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from challenge.manual import risk as risk_mod
from challenge.manual import scanner as scanner_mod
from challenge.manual import journal as journal_mod

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CANDLES = os.path.join(ROOT, "data", "backtest", "candles")
WATCH = ["AAPL", "NVDA", "TSLA", "SPY", "GLD", "COIN", "AMD", "MU", "MRVL", "PLTR"]
DATES = [dt.date(2026, 7, 28), dt.date(2026, 7, 29), dt.date(2026, 7, 30),
         dt.date(2026, 7, 31), dt.date(2026, 8, 3), dt.date(2026, 8, 4),
         dt.date(2026, 8, 5), dt.date(2026, 8, 6), dt.date(2026, 8, 7),
         dt.date(2026, 8, 10), dt.date(2026, 8, 11), dt.date(2026, 8, 12),
         dt.date(2026, 8, 13), dt.date(2026, 8, 14), dt.date(2026, 8, 17),
         dt.date(2026, 8, 18), dt.date(2026, 8, 19)]


class TestRisk(unittest.TestCase):
    def test_profile_params(self):
        p = risk_mod.profile_params(1, "B", 0.0, 1000.0)
        self.assertEqual(p["risk_usd"], 2.5)
        self.assertEqual(p["daily_limit_usd"], 15.0)
        self.assertEqual(p["max_trades"], 3)
        self.assertTrue(p["only_a"])
        self.assertEqual(p["profit_lock_usd"], 20.0)

    def test_drawdown_scaling(self):
        p = risk_mod.profile_params(1, "B", -50.0, 1000.0)   # -5%
        self.assertEqual(p["risk_usd"], 1.5)
        self.assertEqual(p["max_trades"], 1)
        self.assertTrue(p["only_a"])
        p2 = risk_mod.profile_params(1, "B", -20.0, 1000.0)  # -2%
        self.assertEqual(p2["risk_usd"], 2.5)

    def test_stop_day_after_two_losses(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            sm.record_trade(-2.5)
            self.assertEqual(sm.state.status, "active")
            sm.record_trade(-2.5)
            self.assertEqual(sm.state.status, "stop_day")
            ok, _ = sm.can_trade("A")
            self.assertFalse(ok)

    def test_profit_lock(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            self.assertEqual(sm.update_equity(1025.0), "flatten_day")
            self.assertEqual(sm.state.status, "profit_locked")

    def test_pause(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 940.0, 1000.0, dt.datetime.now())
            self.assertEqual(sm.update_equity(940.0), "halt")
            self.assertEqual(sm.state.status, "paused")
            r = sm.start_day(1, "B", 940.0, 1000.0, dt.datetime.now())
            self.assertFalse(r["ok"])

    def test_violation_forces_stop_day(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            sm.record_trade(-2.5, violation="trading after stop-day")
            self.assertEqual(sm.state.status, "stop_day")
            self.assertIn("violation", sm.state.status_reason)

    def test_position_size(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "state.json")
            sm = risk_mod.DailyStateMachine(state_path=p)
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime.now())
            size = sm.position_size(300.0, 299.0, "long")   # $1 stop -> 2.5 shares
            self.assertAlmostEqual(size, 2.5, places=2)


class TestScanner(unittest.TestCase):
    def _candles(self, sym):
        p = os.path.join(CANDLES, sym + ".json")
        if not os.path.exists(p):
            self.skipTest(f"no candles for {sym}")
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def test_resample(self):
        base = [{"time": 100, "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 1},
                {"time": 160, "open": 1.5, "high": 2.5, "low": 1, "close": 2, "volume": 2},
                {"time": 360, "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 3}]
        out = scanner_mod.resample(base, 5)
        self.assertEqual(len(out), 1)  # 100..360 spans 5 min
        self.assertEqual(out[0]["high"], 3)
        self.assertEqual(out[0]["low"], 0)
        self.assertEqual(out[0]["close"], 2.5)
        self.assertEqual(out[0]["volume"], 6)

    def test_ema(self):
        self.assertAlmostEqual(scanner_mod.ema([1, 2, 3, 4, 5], 3)[-1], 4.0625)

    def test_scan_full_watchlist_sane(self):
        tradable = 0
        inverted = 0
        for d in DATES:
            for sym in WATCH:
                candles = self._candles(sym)
                res = scanner_mod.scan_setup(sym, d, candles, dt.time(13, 30), {})
                if res.tradable:
                    tradable += 1
                    if res.bias == "long":
                        self.assertGreater(res.entry, res.stop, f"{d} {sym}")
                    else:
                        self.assertLess(res.entry, res.stop, f"{d} {sym}")
                    self.assertGreaterEqual(res.rr, 2.0)
                    self.assertIn(res.grade, ("A", "B"))
        self.assertGreaterEqual(tradable, 1)
        self.assertEqual(inverted, 0)


class TestJournal(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "j.csv")
            n1 = journal_mod.add_trade(p, "2026-08-19", "14:30", "AAPL", "L", "B",
                                       318.65, 312.30, 331.33, 2.5, 0.25)
            n2 = journal_mod.add_trade(p, "2026-08-19", "14:40", "NVDA", "S", "C",
                                       219, 220, 217, 2.5, 0.25)
            self.assertEqual((n1, n2), (1, 2))
            self.assertTrue(journal_mod.close_trade(p, 1, 5.0, 2.0, "W"))
            self.assertTrue(journal_mod.close_trade(p, 2, -2.5, -1.0, "L"))
            s = journal_mod.daily_summary(p)
            self.assertEqual(s[0]["trades"], 2)
            self.assertAlmostEqual(s[0]["pnl_usd"], 2.5)
            w = journal_mod.weekly_metrics(p)
            self.assertEqual(len(w), 1)
            self.assertAlmostEqual(w[0]["avg_r"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)