"""Tests for latency monitoring and alerting (P1-9)."""
import time
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
import pytest

from usstocks.scanner_loop import SignalOnlyRunner
from tests.fixtures.vwap_scenarios import long_scenario

NY = ZoneInfo("America/New_York")


def test_latency_metrics_recorded_on_scan():
    class MockProvider:
        def get_bars(self, symbol, count):
            return long_scenario()

    class MockNotifier:
        def send_signal(self, s):
            pass

        def send_risk_event(self, e):
            pass

    cfg = {
        "risk": {},
        "challenge": {},
        "strategy": {},
        "scanner": {"warn_latency_threshold_s": 0.001},
        "us_stocks": {"tech_symbols": ["AMD"]},
        "session": {"holidays": []},
    }

    runner = SignalOnlyRunner(
        cfg,
        MockProvider(),
        MockNotifier(),
        watchlist=["AMD"],
        symbol_ids={"AMD": "S", "QQQ": "Q"},
    )

    now = datetime(2026, 8, 27, 10, 0, tzinfo=NY)
    runner.scan_once(now)

    assert runner.metrics["total_scans"] == 1
    assert runner.metrics["last_scan_duration_ms"] >= 0.0
    assert runner.metrics["last_scan_timestamp"] > 0
