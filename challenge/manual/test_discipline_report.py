# -*- coding: utf-8 -*-
"""Tests for discipline report."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from challenge.manual.discipline_report import (
    compute_adherence,
    compute_commission_drag,
    compute_regime_breakdown,
    compute_streak_analysis,
    compute_time_bucket_stats,
    format_report,
    generate_report,
)


def _trade(**overrides) -> dict:
    """Create a minimal trade row with sensible defaults."""
    base = {
        "num": "1",
        "date": "2026-08-20",
        "time": "14:30",
        "instrument": "AAPL",
        "direction": "L",
        "setup_class": "B",
        "entry_price": "150.0",
        "stop": "148.0",
        "target": "154.0",
        "risk_usd": "5.0",
        "risk_pct": "0.5",
        "result_usd": "",
        "result_r": "",
        "outcome": "",
        "by_plan": "да",
        "violation": "",
        "comment": "",
        "commission_usd": "1.0",
        "session_bucket": "prime",
        "time_in_trade_min": "30",
        "volume_ratio": "1.5",
        "regime": "trend_up",
    }
    base.update(overrides)
    return base


class TestAdherence(unittest.TestCase):
    def test_all_complete(self):
        rows = [_trade(), _trade(num="2")]
        a = compute_adherence(rows)
        self.assertEqual(a["total"], 2)
        self.assertEqual(a["complete"], 2)
        self.assertEqual(a["adherence_pct"], 100.0)

    def test_missing_stop(self):
        rows = [_trade(stop="")]
        a = compute_adherence(rows)
        self.assertEqual(a["complete"], 0)
        self.assertEqual(a["adherence_pct"], 0.0)
        self.assertIn("stop", a["missing_fields"])

    def test_empty_rows(self):
        a = compute_adherence([])
        self.assertEqual(a["total"], 0)
        self.assertEqual(a["adherence_pct"], 0.0)

    def test_by_plan(self):
        rows = [_trade(by_plan="да"), _trade(num="2", by_plan="нет")]
        a = compute_adherence(rows)
        self.assertEqual(a["by_plan_pct"], 50.0)


class TestRegimeBreakdown(unittest.TestCase):
    def test_mixed_regimes(self):
        rows = [
            _trade(regime="trend_up", result_r="1.5", outcome="W"),
            _trade(num="2", regime="trend_up", result_r="-1.0", outcome="L"),
            _trade(num="3", regime="range", result_r="0.5", outcome="W"),
        ]
        rb = compute_regime_breakdown(rows)
        self.assertEqual(rb["trend_up"]["n"], 2)
        self.assertEqual(rb["trend_up"]["win_rate_pct"], 50.0)
        self.assertEqual(rb["range"]["n"], 1)
        self.assertAlmostEqual(rb["trend_up"]["avg_r"], 0.25)

    def test_empty(self):
        rb = compute_regime_breakdown([])
        self.assertEqual(len(rb), 0)


class TestTimeBucketStats(unittest.TestCase):
    def test_prime_vs_degraded(self):
        rows = [
            _trade(session_bucket="prime", result_r="2.0", outcome="W", commission_usd="1.0", time_in_trade_min="45"),
            _trade(
                num="2",
                session_bucket="prime",
                result_r="-1.0",
                outcome="L",
                commission_usd="1.0",
                time_in_trade_min="20",
            ),
            _trade(
                num="3",
                session_bucket="degraded",
                result_r="-0.5",
                outcome="L",
                commission_usd="1.5",
                time_in_trade_min="10",
            ),
        ]
        tb = compute_time_bucket_stats(rows)
        self.assertEqual(tb["prime"]["n"], 2)
        self.assertEqual(tb["prime"]["win_rate_pct"], 50.0)
        self.assertAlmostEqual(tb["prime"]["avg_r"], 0.5)
        self.assertAlmostEqual(tb["prime"]["total_commission"], 2.0)
        self.assertEqual(tb["degraded"]["n"], 1)


class TestCommissionDrag(unittest.TestCase):
    def test_basic(self):
        rows = [
            _trade(result_usd="10.0", commission_usd="2.0"),
            _trade(num="2", result_usd="-5.0", commission_usd="1.0"),
            _trade(num="3", result_usd="8.0", commission_usd="1.5"),
        ]
        cd = compute_commission_drag(rows)
        self.assertAlmostEqual(cd["total_commission"], 4.5)
        self.assertAlmostEqual(cd["gross_wins"], 18.0)
        self.assertAlmostEqual(cd["net_pnl"], 13.0)
        # drag = 4.5 / 18.0 * 100 = 25%
        self.assertAlmostEqual(cd["commission_drag_pct"], 25.0, places=0)

    def test_no_wins(self):
        rows = [_trade(result_usd="-5.0", commission_usd="1.0")]
        cd = compute_commission_drag(rows)
        self.assertEqual(cd["commission_drag_pct"], 0.0)


class TestStreakAnalysis(unittest.TestCase):
    def test_loss_streak(self):
        rows = [
            _trade(outcome="W"),
            _trade(num="2", outcome="L"),
            _trade(num="3", outcome="L"),
            _trade(num="4", outcome="L"),
            _trade(num="5", outcome="W"),
            _trade(num="6", outcome="L"),
        ]
        st = compute_streak_analysis(rows)
        self.assertEqual(st["max_loss_streak"], 3)
        self.assertEqual(st["max_win_streak"], 1)

    def test_drawdown(self):
        rows = [
            _trade(result_r="-1.0"),
            _trade(num="2", result_r="-1.0"),
            _trade(num="3", result_r="2.0"),
        ]
        st = compute_streak_analysis(rows)
        self.assertAlmostEqual(st["max_drawdown_r"], 2.0)


class TestGenerateReport(unittest.TestCase):
    def test_with_temp_journal(self):
        with tempfile.TemporaryDirectory() as td:
            from challenge.manual.journal import add_trade

            p = os.path.join(td, "j.csv")
            add_trade(
                p,
                "2026-08-20",
                "14:30",
                "AAPL",
                "L",
                "B",
                150.0,
                148.0,
                154.0,
                5.0,
                0.5,
                commission_usd=1.0,
                session_bucket="prime",
                regime="trend_up",
            )
            add_trade(
                p,
                "2026-08-20",
                "14:40",
                "NVDA",
                "S",
                "A",
                120.0,
                122.0,
                116.0,
                5.0,
                0.5,
                result_usd=-5.0,
                result_r=-1.0,
                outcome="L",
                commission_usd=1.5,
                session_bucket="prime",
                regime="range",
            )
            report = generate_report(p)
            self.assertEqual(report["trade_count"], 2)
            self.assertIn("adherence", report)
            self.assertIn("regime_breakdown", report)
            self.assertIn("commission_drag", report)

    def test_format_report(self):
        report = {
            "as_of": "2026-08-22T12:00:00+00:00",
            "trade_count": 3,
            "adherence": {"total": 3, "complete": 3, "adherence_pct": 100.0, "missing_fields": {}, "by_plan_pct": 66.7},
            "regime_breakdown": {
                "trend_up": {"n": 2, "wins": 1, "win_rate_pct": 50.0, "avg_r": 0.25, "net_pnl": 5.0, "total_r": 0.5},
                "range": {"n": 1, "wins": 0, "win_rate_pct": 0.0, "avg_r": -1.0, "net_pnl": -5.0, "total_r": -1.0},
            },
            "time_bucket_stats": {
                "prime": {
                    "n": 3,
                    "wins": 1,
                    "win_rate_pct": 33.3,
                    "avg_r": -0.25,
                    "net_pnl": 0.0,
                    "total_commission": 3.0,
                    "avg_time_min": 30.0,
                }
            },
            "commission_drag": {
                "total_commission": 3.0,
                "avg_commission_per_trade": 1.0,
                "gross_wins": 10.0,
                "gross_losses": 10.0,
                "net_pnl": 0.0,
                "commission_drag_pct": 30.0,
                "commission_drag_of_net_pct": 0.0,
            },
            "streak": {"max_loss_streak": 2, "max_win_streak": 1, "max_drawdown_r": 2.0, "current_streak": 0},
        }
        text = format_report(report)
        self.assertIn("JOURNAL ADHERENCE", text)
        self.assertIn("REGIME BREAKDOWN", text)
        self.assertIn("COMMISSION DRAG", text)
        self.assertIn("STREAK ANALYSIS", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
