# -*- coding: utf-8 -*-
"""Tests for session quality score."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from challenge.manual.quality_score import (
    _regime_score,
    _time_of_day_score,
    _volume_score,
    compute_quality_score,
    format_quality,
)


def _ts(h, m=0):
    return int(dt.datetime(2026, 8, 20, h, m, tzinfo=dt.UTC).timestamp())


class TestTimeOfDay(unittest.TestCase):
    def test_prime_window(self):
        """14:00-15:00 UTC (19:00-20:00 local) = prime = 30 pts."""
        score = _time_of_day_score(_ts(14, 30))
        self.assertEqual(score, 30)

    def test_good_window(self):
        """15:00-16:30 UTC = good = 20 pts."""
        score = _time_of_day_score(_ts(16, 0))
        self.assertEqual(score, 20)

    def test_early_window(self):
        """13:30-14:00 UTC = early = 15 pts."""
        score = _time_of_day_score(_ts(13, 45))
        self.assertEqual(score, 15)

    def test_late_window(self):
        """16:30-19:10 UTC = late = 10 pts."""
        score = _time_of_day_score(_ts(18, 0))
        self.assertEqual(score, 10)

    def test_degraded_window(self):
        """19:10-19:55 UTC = degraded = 0 pts."""
        score = _time_of_day_score(_ts(19, 30))
        self.assertEqual(score, 0)

    def test_outside_session(self):
        """12:00 UTC = outside session = 0 pts."""
        score = _time_of_day_score(_ts(12, 0))
        self.assertEqual(score, 0)


class TestVolume(unittest.TestCase):
    def test_high_volume(self):
        self.assertEqual(_volume_score(2.5), 40)
        self.assertEqual(_volume_score(2.0), 40)

    def test_strong_volume(self):
        self.assertEqual(_volume_score(1.7), 35)
        self.assertEqual(_volume_score(1.5), 35)

    def test_above_avg(self):
        self.assertEqual(_volume_score(1.3), 25)

    def test_average(self):
        self.assertEqual(_volume_score(1.0), 15)

    def test_below_avg(self):
        self.assertEqual(_volume_score(0.9), 5)
        self.assertEqual(_volume_score(0.5), 0)


class TestRegime(unittest.TestCase):
    def test_aligned_trend(self):
        self.assertEqual(_regime_score("trend_up", "long"), 30)
        self.assertEqual(_regime_score("trend_down", "short"), 30)

    def test_opposed_trend(self):
        self.assertEqual(_regime_score("trend_up", "short"), 5)
        self.assertEqual(_regime_score("trend_down", "long"), 5)

    def test_range(self):
        self.assertEqual(_regime_score("range", "long"), 15)

    def test_compression(self):
        self.assertEqual(_regime_score("compression", "long"), 10)

    def test_unknown(self):
        self.assertEqual(_regime_score("", "long"), 12)


class TestComposite(unittest.TestCase):
    def test_perfect_score(self):
        """Prime time + high volume + aligned regime = near-perfect."""
        s = compute_quality_score(
            signal_ts=_ts(14, 30),  # prime
            volume_ratio=2.5,       # exceptional
            regime="trend_up",
            bias="long",
        )
        self.assertGreaterEqual(s["total"], 95)
        self.assertEqual(s["grade"], "A")
        self.assertEqual(s["volume"], 40)
        self.assertEqual(s["time_of_day"], 30)
        self.assertEqual(s["regime"], 30)

    def test_low_score(self):
        """Degraded time + low volume + compression = very low."""
        s = compute_quality_score(
            signal_ts=_ts(19, 30),  # degraded
            volume_ratio=0.5,       # weak
            regime="compression",
            bias="long",
        )
        self.assertLessEqual(s["total"], 20)
        self.assertEqual(s["grade"], "D")

    def test_no_data_defaults(self):
        """With no volume/regime data, score is based on time only."""
        s = compute_quality_score(signal_ts=_ts(14, 30))
        self.assertEqual(s["time_of_day"], 30)
        self.assertEqual(s["volume"], 15)  # default 1.0x = average
        self.assertEqual(s["total"], 57)   # 15 + 30 + 12


class TestFormat(unittest.TestCase):
    def test_format(self):
        s = compute_quality_score(_ts(14, 30), 2.0, "trend_up", "long")
        text = format_quality(s)
        self.assertIn("Quality", text)
        self.assertIn("A", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
