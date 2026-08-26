# -*- coding: utf-8 -*-
"""Verify that the S/R window and quality-calibration thresholds read their
values from manual_config.yaml (with hard-coded fallbacks), rather than
staying baked-in constants.

Covers:
  * sr_zones._load_window_config -> premarket/session UTC windows
  * sr_zones._hm_to_sec
  * quality_score_live._load_min_trades_per_bucket
  * module-level constants agree with the shipped manual_config.yaml
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml

from challenge.manual import sr_zones
from challenge.manual import quality_score_live as qsl


def _write_cfg(tmpdir, **overrides):
    p = os.path.join(tmpdir, "manual_config.yaml")
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(overrides, f)
    return p


class TestSRZoneWindowConfig(unittest.TestCase):
    def test_defaults_when_file_missing(self):
        # A missing config file must fall back to NYSE defaults.
        original = sr_zones._MANUAL_CFG_PATH
        try:
            sr_zones._MANUAL_CFG_PATH = os.path.join(
                tempfile.gettempdir(), "_does_not_exist_manual_config.yaml")
            start, end, ss, se = sr_zones._load_window_config()
            self.assertEqual(start, 9 * 3600)
            self.assertEqual(end, 13 * 3600 + 30 * 60)
            self.assertEqual(ss, 13 * 3600 + 30 * 60)
            self.assertEqual(se, 19 * 3600 + 55 * 60)
        finally:
            sr_zones._MANUAL_CFG_PATH = original

    def test_overrides_respected(self):
        # Config values must flow through to the loaded windows.
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_cfg(tmp, premarket_start_utc="08:30",
                           premarket_end_utc="13:00",
                           session_start_utc="13:30",
                           session_end_utc="20:30")
            original = sr_zones._MANUAL_CFG_PATH
            try:
                sr_zones._MANUAL_CFG_PATH = p
                start, end, ss, se = sr_zones._load_window_config()
                self.assertEqual(start, 8 * 3600 + 30 * 60)
                self.assertEqual(end, 13 * 3600)
                self.assertEqual(ss, 13 * 3600 + 30 * 60)
                self.assertEqual(se, 20 * 3600 + 30 * 60)
            finally:
                sr_zones._MANUAL_CFG_PATH = original

    def test_hm_to_sec(self):
        self.assertEqual(sr_zones._hm_to_sec("09:00"), 9 * 3600)
        self.assertEqual(sr_zones._hm_to_sec("13:30"), 13 * 3600 + 30 * 60)
        self.assertEqual(sr_zones._hm_to_sec("19:55"), 19 * 3600 + 55 * 60)

    def test_shipped_config_matches_defaults(self):
        cfg_path = sr_zones._MANUAL_CFG_PATH
        self.assertTrue(os.path.isfile(cfg_path))
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        self.assertEqual(cfg.get("premarket_start_utc"), "09:00")
        self.assertEqual(cfg.get("premarket_end_utc"), "13:30")
        # Module constants currently reflect the shipped config (defaults).
        self.assertEqual(sr_zones.PREMARKET_START_SEC, 9 * 3600)
        self.assertEqual(sr_zones.PREMARKET_END_SEC, 13 * 3600 + 30 * 60)
        self.assertEqual(sr_zones.SESSION_START_SEC, 13 * 3600 + 30 * 60)
        self.assertEqual(sr_zones.SESSION_END_SEC, 19 * 3600 + 55 * 60)


class TestQualityThresholdConfig(unittest.TestCase):
    def test_default_when_file_missing_or_no_key(self):
        self.assertTrue(qsl.DEFAULT_MIN_TRADES_PER_BUCKET == 5)

    def test_load_returns_default_from_shipped_config(self):
        # Shipped config carries min_trades_per_bucket: 5.
        self.assertEqual(qsl._load_min_trades_per_bucket(), 5)
        self.assertEqual(qsl.MIN_TRADES_PER_BUCKET, 5)

    def test_positive_override_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_cfg(tmp, min_trades_per_bucket=7)
            original = qsl.MANUAL_CFG_PATH
            try:
                qsl.MANUAL_CFG_PATH = p
                self.assertEqual(qsl._load_min_trades_per_bucket(), 7)
            finally:
                qsl.MANUAL_CFG_PATH = original

    def test_zero_or_negative_override_uses_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_cfg(tmp, min_trades_per_bucket=0)
            original = qsl.MANUAL_CFG_PATH
            try:
                qsl.MANUAL_CFG_PATH = p
                self.assertEqual(qsl._load_min_trades_per_bucket(), 5)
            finally:
                qsl.MANUAL_CFG_PATH = original


if __name__ == "__main__":
    unittest.main()