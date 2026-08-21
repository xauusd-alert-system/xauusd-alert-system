# -*- coding: utf-8 -*-
"""Tests for S/R zone detection and proximity filter."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from challenge.manual.sr_zones import (
    detect_sr_zones, check_proximity, format_zones,
    SRZone, _resample_5min, _swing_points, _cluster_zones,
)


def _bar(ts, o, h, l, c, v=1000):
    return {"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _ts(y, m, d, h, mi=0):
    return int(dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc).timestamp())


class TestSRZoneDetection(unittest.TestCase):
    def test_prev_day_levels(self):
        """Previous day high/low/close should create zones."""
        # Day 1 (prior): session 13:30-19:55 UTC
        day1 = [_bar(_ts(2026, 8, 19, 13, 30), 100, 105, 99, 103),
                _bar(_ts(2026, 8, 19, 19, 55), 103, 104, 102, 102)]
        # Day 2 (current): some bars to detect zones from
        day2 = [_bar(_ts(2026, 8, 20, 9, 0), 102, 103, 101, 102),
                _bar(_ts(2026, 8, 20, 13, 30), 102, 104, 101, 103)]
        candles = day1 + day2
        zones = detect_sr_zones(candles, dt.date(2026, 8, 20))
        self.assertGreater(len(zones), 0)
        # Check high zone exists (resistance at ~105)
        high_zones = [z for z in zones if z.direction == "resistance" and z.price >= 104]
        self.assertGreater(len(high_zones), 0)
        self.assertGreaterEqual(high_zones[0].price, 104.0)

    def test_premarket_levels(self):
        """Today's premarket high/low should create zones."""
        pm = [_bar(_ts(2026, 8, 20, 9, 0), 100, 102, 99, 101),
              _bar(_ts(2026, 8, 20, 10, 0), 101, 104, 100, 103),
              _bar(_ts(2026, 8, 20, 12, 0), 103, 103, 101, 101)]
        session = [_bar(_ts(2026, 8, 20, 14, 0), 101, 102, 100, 101)]
        candles = pm + session
        zones = detect_sr_zones(candles, dt.date(2026, 8, 20))
        # Clustering may merge nearby levels; check by direction + price range
        pm_high = [z for z in zones if z.direction == "resistance" and z.price >= 103]
        pm_low = [z for z in zones if z.direction == "support" and z.price <= 100]
        self.assertGreater(len(pm_high), 0)
        self.assertGreater(len(pm_low), 0)
        self.assertAlmostEqual(pm_high[0].price, 104.0, places=1)
        self.assertAlmostEqual(pm_low[0].price, 99.0, places=1)

    def test_no_zones_with_no_data(self):
        """Empty data should return no zones."""
        zones = detect_sr_zones([], dt.date(2026, 8, 20))
        self.assertEqual(len(zones), 0)


class TestProximityFilter(unittest.TestCase):
    def test_long_too_close_to_resistance(self):
        """Long entry near resistance should be rejected."""
        zones = [SRZone(105.0, "prev_high", "resistance", 2, 0.7)]
        ok, reason = check_proximity(104.5, 103.0, 108.0, "long", zones, buffer_usd=2.0)
        self.assertFalse(ok)
        self.assertIn("resistance", reason)

    def test_long_ok_above_support(self):
        """Long entry well above support should pass."""
        zones = [SRZone(95.0, "prev_low", "support", 1, 0.5)]
        ok, reason = check_proximity(100.0, 98.0, 104.0, "long", zones, buffer_usd=2.0)
        self.assertTrue(ok)

    def test_short_too_close_to_support(self):
        """Short entry near support should be rejected."""
        zones = [SRZone(95.0, "prev_low", "support", 2, 0.7)]
        ok, reason = check_proximity(95.5, 97.0, 91.0, "short", zones, buffer_usd=2.0)
        self.assertFalse(ok)
        self.assertIn("support", reason)

    def test_no_zones_passes(self):
        """No zones → always pass."""
        ok, reason = check_proximity(100.0, 98.0, 104.0, "long", [], buffer_usd=2.0)
        self.assertTrue(ok)

    def test_zone_far_away_passes(self):
        """Zone far from entry should pass."""
        zones = [SRZone(120.0, "prev_high", "resistance", 1, 0.5)]
        ok, reason = check_proximity(100.0, 98.0, 104.0, "long", zones, buffer_usd=2.0)
        self.assertTrue(ok)

    def test_multiple_zones(self):
        """Multiple zones: should fail on the closest problematic one."""
        zones = [SRZone(102.0, "prev_high", "resistance", 1, 0.5),
                 SRZone(95.0, "prev_low", "support", 1, 0.5)]
        # Entry at 101.5 — within 2 of resistance at 102
        ok, _ = check_proximity(101.5, 100.0, 105.0, "long", zones, buffer_usd=2.0)
        self.assertFalse(ok)


class TestClustering(unittest.TestCase):
    def test_nearby_levels_cluster(self):
        """Two levels within tolerance should merge."""
        levels = [(100.0, "prev_high", "2026-08-19"),
                  (100.15, "premarket_high", "2026-08-20")]
        zones = _cluster_zones(levels, tolerance_pct=0.002)
        self.assertEqual(len(zones), 1)
        self.assertAlmostEqual(zones[0].price, 100.075, places=2)
        self.assertEqual(zones[0].strength, 2)

    def test_far_levels_dont_cluster(self):
        """Two levels beyond tolerance should stay separate."""
        levels = [(100.0, "prev_high", "2026-08-19"),
                  (103.0, "prev_high", "2026-08-18")]
        zones = _cluster_zones(levels, tolerance_pct=0.002)
        self.assertEqual(len(zones), 2)


class TestFormatZones(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(format_zones([]), "  (no zones detected)")

    def test_with_zones(self):
        zones = [SRZone(105.0, "prev_high", "resistance", 1, 0.5),
                 SRZone(95.0, "prev_low", "support", 2, 0.7)]
        text = format_zones(zones)
        self.assertIn("105.00", text)
        self.assertIn("95.00", text)
        self.assertIn("resistance", text)
        self.assertIn("support", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
