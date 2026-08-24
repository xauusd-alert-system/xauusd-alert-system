# -*- coding: utf-8 -*-
"""Tests for quality_autocal module."""
import json, os, unittest, datetime as dt
from pathlib import Path

import sys
sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")

from challenge.manual.quality_autocal import (
    find_optimal_threshold, collect_live_data, recalibrate,
)


class TestOptimalThreshold(unittest.TestCase):
    def test_empty_returns_zero(self):
        self.assertEqual(find_optimal_threshold([], min_trades=5), 0)

    def test_too_few_returns_zero(self):
        pairs = [(80, 2.0), (90, 3.5)]  # only 2, less than min_trades=5
        self.assertEqual(find_optimal_threshold(pairs, min_trades=5), 0)

    def test_high_quality_wins(self):
        """Good quality -> wins, low -> losses. Should pick high threshold."""
        pairs = []
        for i in range(20):
            qs = i * 5
            r = 3.5 if qs >= 60 else -1.0
            pairs.append((qs, r))
        best = find_optimal_threshold(pairs, min_trades=5)
        self.assertGreaterEqual(best, 55, f"Expected high threshold, got {best}")

    def test_no_predictive_power(self):
        """When quality has no correlation with R, threshold should be 0."""
        pairs = [(0, 2.0), (100, -1.0), (50, -1.0), (80, 3.5), (20, -1.0),
                 (90, -1.0), (10, 3.5), (30, -1.0)]  # mixed, no clear pattern
        best = find_optimal_threshold(pairs, min_trades=3)
        # Should either be 0 (keep all) or low, not high
        self.assertLess(best, 80, f"Unexpected high threshold: {best}")

    def test_monotonic(self):
        """Perfect monotonic: Q>=50 is always +3.5, Q<50 is always -1.0."""
        pairs = []
        for qs in range(0, 100, 5):
            r = 3.5 if qs >= 50 else -1.0
            pairs.append((qs, r))
        best = find_optimal_threshold(pairs, min_trades=5)
        # Should be exactly 50
        self.assertIn(best, (45, 50, 55), f"Expected ~50, got {best}")


class TestAutoCal(unittest.TestCase):
    def test_recalibrate_no_data(self):
        """Recalibration with no live data should return existing thresholds."""
        result = recalibrate()
        self.assertIn("impulse", result)
        self.assertIn("gap_fade", result)
        self.assertIn("opening_drive", result)
        self.assertIn("calibrated_at", result)
        # Should have kept existing values since no live data
        for stype in ("impulse", "gap_fade", "opening_drive"):
            self.assertGreaterEqual(result[stype], 0)
            self.assertLessEqual(result[stype], 100)


if __name__ == "__main__":
    unittest.main()