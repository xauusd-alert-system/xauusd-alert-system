# -*- coding: utf-8 -*-
"""Regression tests for the US Stocks Headliners analysis-only manual system."""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest

from challenge.manual import outcomes, risk, scanner


class RiskTests(unittest.TestCase):
    def test_whole_share_size_includes_minimum_fees(self):
        # One share with a $1 stop has at least $2 round-trip fees.
        self.assertEqual(risk.max_safe_shares(100.0, 99.0, 2.5), 0)
        self.assertEqual(risk.max_safe_shares(100.0, 99.0, 3.0), 1)

    def test_stage_two_drawdown_requires_one_a_setup_at_four_percent(self):
        params = risk.effective_profile(2, "B", -40.0, 1000.0)
        self.assertEqual(params["risk_usd"], 1.5)
        self.assertEqual(params["max_trades"], 1)
        self.assertTrue(params["only_a"])

    def test_profile_a_requires_confirmed_stage_profit_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.json")
            sm = risk.DailyStateMachine(state_path=state_path)
            rejected = sm.start_day(1, "A", 1000.0, 1000.0, dt.datetime(2026, 8, 21), True)
            self.assertFalse(rejected["ok"])
            accepted = sm.start_day(1, "A", 1025.0, 1000.0, dt.datetime(2026, 8, 21), True)
            self.assertTrue(accepted["ok"])

    def test_b_setup_requires_positive_day_and_is_limited_to_one(self):
        with tempfile.TemporaryDirectory() as directory:
            sm = risk.DailyStateMachine(state_path=os.path.join(directory, "state.json"))
            self.assertTrue(sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime(2026, 8, 21))["ok"])
            self.assertFalse(sm.can_trade("B")[0])
            sm.update_equity(1001.0)
            self.assertTrue(sm.can_trade("B")[0])
            sm.record_trade(0.5, setup_class="B")
            self.assertFalse(sm.can_trade("B")[0])

    def test_one_open_position_and_near_close_are_no_go(self):
        with tempfile.TemporaryDirectory() as directory:
            sm = risk.DailyStateMachine(state_path=os.path.join(directory, "state.json"))
            sm.start_day(1, "B", 1000.0, 1000.0, dt.datetime(2026, 8, 21))
            self.assertFalse(sm.can_trade("A", open_positions=1)[0])
            self.assertFalse(sm.can_trade("A", minutes_to_close=44)[0])


class ScannerTests(unittest.TestCase):
    def test_cross_midnight_session_bounds_are_utc_correct(self):
        cfg = {"session_timezone_offset_minutes": 240,
               "session_start_local": "18:30", "session_end_local": "00:55"}
        start, end = scanner.session_bounds(dt.date(2026, 8, 21), cfg)
        self.assertEqual(end - start, 6 * 3600 + 25 * 60)
        self.assertEqual(dt.datetime.fromtimestamp(start, dt.timezone.utc).strftime("%H:%M"), "14:30")
        self.assertEqual(dt.datetime.fromtimestamp(end, dt.timezone.utc).strftime("%H:%M"), "20:55")

    def test_as_of_excludes_future_candles(self):
        base = int(dt.datetime(2026, 8, 21, 14, 30, tzinfo=dt.timezone.utc).timestamp())
        candles = []
        for index in range(80):
            price = 100 + index * 0.01
            candles.append({"time": base + index * 60, "open": price, "high": price + 0.02,
                            "low": price - 0.02, "close": price + 0.01, "volume": 100})
        future = {"time": base + 10000, "open": 999, "high": 1000, "low": 1, "close": 2, "volume": 999999}
        cfg = {"session_timezone_offset_minutes": 240, "session_start_local": "18:30",
               "session_end_local": "00:55", "calendar_status": "verified"}
        left = scanner.scan_setup("TEST", dt.date(2026, 8, 21), candles, cfg=cfg, as_of_ts=candles[-1]["time"])
        right = scanner.scan_setup("TEST", dt.date(2026, 8, 21), candles + [future], cfg=cfg, as_of_ts=candles[-1]["time"])
        self.assertEqual(left.no_go, right.no_go)
        self.assertEqual(left.entry, right.entry)


class OutcomeTests(unittest.TestCase):
    def test_partial_one_r_then_break_even_returns_half_r(self):
        signal = int(dt.datetime(2026, 8, 21, 15, 30, tzinfo=dt.timezone.utc).timestamp())
        candles = [
            {"time": signal + 60, "open": 100, "high": 101.1, "low": 100.2, "close": 100.9},
            {"time": signal + 120, "open": 100.9, "high": 100.95, "low": 99.95, "close": 100.0},
        ]
        outcome, realised_r, _ = outcomes.simulate_outcome(
            signal, 100.0, 99.0, 102.0, "long", candles,
            now_ts=signal + 3 * 60,
        )
        self.assertEqual(outcome, "r1_be")
        self.assertEqual(realised_r, 0.5)

    def test_partial_one_r_then_two_r_returns_one_point_five_r(self):
        signal = int(dt.datetime(2026, 8, 21, 15, 30, tzinfo=dt.timezone.utc).timestamp())
        candles = [
            {"time": signal + 60, "open": 100, "high": 101.1, "low": 100.2, "close": 100.9},
            {"time": signal + 120, "open": 100.9, "high": 102.1, "low": 100.9, "close": 102.0},
        ]
        outcome, realised_r, _ = outcomes.simulate_outcome(
            signal, 100.0, 99.0, 102.0, "long", candles,
            now_ts=signal + 3 * 60,
        )
        self.assertEqual(outcome, "r1_r2")
        self.assertEqual(realised_r, 1.5)


class BoundaryTests(unittest.TestCase):
    def test_manual_package_contains_no_order_routing_or_browser_automation(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        blocked = ("place_order(", "close_position(", ".flatten(", "playwright", "selenium", "challenge_tokens")
        for directory, _, files in os.walk(root):
            for name in files:
                if not name.endswith(".py") or name.startswith("test_"):
                    continue
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    source = f.read().lower()
                for marker in blocked:
                    self.assertNotIn(marker.lower(), source, f"forbidden marker {marker} in {name}")


if __name__ == "__main__":
    unittest.main()
