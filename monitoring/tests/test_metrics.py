"""ТЗ 6.1: MetricsCollector + /api/execution-metrics endpoint tests.

Covers:
    - collector_counting            — counters, fill rate, timings, threads;
    - jsonl_append_and_rotation     — sink appends and rotates by size;
    - endpoint_returns_aggregates   — TestClient payload (auth-guarded);
    - rejected_reasons_recorded     — reason-code accounting + executor hook.
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from monitoring.metrics import MetricsCollector, _percentile


# --------------------------------------------------------- collector_counting

def test_collector_counting():
    c = MetricsCollector()
    c.record("orders_sent")
    c.record("orders_sent")
    c.record("orders_filled")
    c.record_timing("submit_group", 12.5)
    c.record_timing("submit_group", 30.0)
    c.record_poll(100.0, mt5_calls=5)

    s = c.summary()
    assert s["orders_sent"] == 2
    assert s["orders_filled"] == 1
    assert s["fill_rate_pct"] == 50.0
    assert s["stages"]["submit_group"]["count"] == 2
    assert s["stages"]["submit_group"]["avg_ms"] == pytest.approx(21.25)
    assert s["poll_count"] == 1
    assert s["poll_avg_ms"] == pytest.approx(100.0)
    assert s["mt5_calls_avg"] == pytest.approx(5.0)
    assert s["uptime_s"] >= 0.0


def test_collector_percentile_helper():
    assert _percentile([], 95) == 0.0
    assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95) == 5.0
    assert _percentile([1.0], 95) == 1.0


def test_collector_thread_safety():
    c = MetricsCollector(poll_window=5000, stage_history=5000)

    def _worker():
        for _ in range(200):
            c.record("orders_sent")
            c.record_timing("poll_once", 1.0)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    s = c.summary()
    assert s["orders_sent"] == 8 * 200
    assert s["stages"]["poll_once"]["count"] == 8 * 200


# -------------------------------------------------- jsonl_append_and_rotation

def test_jsonl_append_and_rotation(tmp_path):
    sink = tmp_path / "metrics.jsonl"
    c = MetricsCollector(
        jsonl_path=str(sink), sink_max_bytes=2000, sink_backups=2,
    )
    c.record("orders_sent")
    c.flush_summary()
    c.record("orders_filled")
    c.flush_summary()

    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert "orders_sent" in first

    # Force rotation: shrink the threshold and keep appending — each summary
    # line is ~600 bytes, so ~4 more flushes cross the 2000-byte limit.
    c._sink_max_bytes = len(sink.read_text(encoding="utf-8")) + 1
    for _ in range(5):
        c.record("orders_sent")
        c.flush_summary()
    rotated = [p for p in tmp_path.iterdir() if p.name.startswith("metrics.jsonl")]
    assert len(rotated) >= 2, "expected at least one rotated sink file"
    # Active sink + at most 2 backups (sink_backups=2).
    assert len(rotated) <= 3


def test_jsonl_sink_fail_open(tmp_path, monkeypatch):
    sink = tmp_path / "metrics.jsonl"
    c = MetricsCollector(jsonl_path=str(sink))
    # Point the sink at a directory path -> open() raises OSError -> fail-open.
    c._jsonl_path = str(tmp_path)
    assert c.flush_summary() is None


def test_flush_summary_disabled_without_path():
    c = MetricsCollector(jsonl_path=None)
    assert c.flush_summary() is None


# ------------------------------------------------- endpoint_returns_aggregates

def test_endpoint_returns_aggregates(monkeypatch):
    from fastapi.testclient import TestClient
    import realtime.app as app_mod
    from realtime.app import app

    collector = MetricsCollector()
    collector.record("orders_sent", 10)
    collector.record("orders_filled", 9)
    monkeypatch.setattr(app_mod, "EXECUTION_METRICS", collector)

    client = TestClient(app)
    res = client.get("/api/execution-metrics")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["orders_sent"] == 10
    assert body["orders_filled"] == 9
    assert body["fill_rate_pct"] == 90.0


def test_endpoint_is_not_public_when_auth_required(monkeypatch):
    """ТЗ 6.1: the endpoint must sit behind the global bearer gate."""
    from fastapi.testclient import TestClient
    import realtime.app as app_mod
    from realtime.app import app

    monkeypatch.setenv("API_AUTH_TOKEN", "tok-123")
    monkeypatch.setenv("API_REQUIRE_AUTH", "1")
    try:
        client = TestClient(app)
        assert client.get("/api/execution-metrics").status_code == 401
        ok = client.get(
            "/api/execution-metrics",
            headers={"Authorization": "Bearer tok-123"},
        )
        assert ok.status_code == 200
    finally:
        monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("API_REQUIRE_AUTH", raising=False)


# ------------------------------------------------- rejected_reasons_recorded

def test_rejected_reasons_recorded():
    c = MetricsCollector()
    c.record("rejected:SIGNAL_EXPIRED")
    c.record("rejected:SIGNAL_EXPIRED")
    c.record("rejected:RISK_LIMIT_EXCEEDED")

    s = c.summary()
    assert s["orders_rejected"] == 3
    assert s["reject_reasons"] == {"SIGNAL_EXPIRED": 2, "RISK_LIMIT_EXCEEDED": 1}


def test_executor_rejection_and_submit_instrumented(tmp_path, monkeypatch):
    """Executor hooks: create_group + submit rejection record metrics."""
    import sys as _sys
    import os as _os

    _root = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", ".."))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)

    from execution.mt5_trade_group import MT5TradeGroupExecutor
    from execution.tests.test_mt5_trade_group import FakeMT5, make_spec

    collector = MetricsCollector()
    monkeypatch.setattr(
        "execution.mt5_trade_group._metrics_record",
        lambda metric, value=1, **extra: collector.record(metric, value, **extra),
    )
    monkeypatch.setattr(
        "execution.mt5_trade_group._metrics_timing",
        lambda stage, ms, **extra: collector.record_timing(stage, ms, **extra),
    )

    db_path = str(tmp_path / "exec.sqlite")
    ex = MT5TradeGroupExecutor(db_path, allow_demo=True,
                               mt5=FakeMT5(), deployment_mode="demo_systematic")
    spec = make_spec()
    # Rejection path: an already-expired TTL is caught at submission and
    # rejected with reason_code=SIGNAL_EXPIRED (broker gates all pass on the
    # FakeMT5 demo double).
    object.__setattr__(spec, "expires_at_utc_ms", 1)
    ex.create_group(spec)
    state = ex.submit_group(spec.group_id)
    assert str(state) == "GroupState.REJECTED" or getattr(state, "name", "") == "REJECTED" \
        or "REJECT" in str(state).upper()

    s = collector.summary()
    assert s["groups_created"] == 1
    assert s["orders_rejected"] >= 1
    assert s["reject_reasons"]
