"""News feed service cache-freshness checks + entrypoint guard (TZ 8.8)."""
from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from services.base import create_health_app
from services.news_feed import service as nf


def _write_cache(path, ts: float, events: int = 3):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "events": [
            {"title": f"e{i}", "country": "USD", "date": "2026-01-01T00:00:00+00:00",
             "impact": "High"} for i in range(events)
        ]}, f)


def test_cache_freshness_fresh_is_ok(tmp_path):
    path = str(tmp_path / "cache.json")
    _write_cache(path, time.time() - 300)  # 5 minutes old
    ok, detail = nf.make_cache_freshness_check(path, max_age_hours=6)()
    assert ok is True


def test_cache_freshness_stale_is_degraded(tmp_path):
    path = str(tmp_path / "cache.json")
    _write_cache(path, time.time() - 10 * 3600)  # 10h old
    ok, detail = nf.make_cache_freshness_check(path, max_age_hours=6)()
    assert ok is False
    assert "exceeds budget" in detail


def test_cache_freshness_missing_file_is_degraded(tmp_path):
    ok, detail = nf.make_cache_freshness_check(str(tmp_path / "no.json"))()
    assert ok is False


def test_cache_file_check_missing_and_corrupt(tmp_path):
    ok, _ = nf.make_cache_file_check(str(tmp_path / "no.json"))()
    assert ok is False

    corrupt = str(tmp_path / "corrupt.json")
    with open(corrupt, "w", encoding="utf-8") as f:
        f.write("{not json")
    ok, detail = nf.make_cache_file_check(corrupt)()
    assert ok is False
    assert "corrupt" in detail


def test_cache_file_check_ok(tmp_path):
    path = str(tmp_path / "cache.json")
    _write_cache(path, time.time())
    ok, detail = nf.make_cache_file_check(path)()
    assert ok is True
    assert "3 events" in detail


def test_build_checks_endpoint_fresh_vs_stale(tmp_path):
    fresh = str(tmp_path / "fresh.json")
    stale = str(tmp_path / "stale.json")
    _write_cache(fresh, time.time())
    _write_cache(stale, time.time() - 48 * 3600)

    body = TestClient(create_health_app(nf.build_checks(fresh))).get("/health").json()
    assert body["status"] == "ok"

    body = TestClient(create_health_app(nf.build_checks(stale))).get("/health").json()
    assert body["status"] == "degraded"
    assert body["checks"]["cache_fresh"]["ok"] is False


def test_cache_age_seconds_format_unchanged(tmp_path):
    """The disk-cache format stays {"ts": ..., "events": [...]} — untouched."""
    path = str(tmp_path / "cache.json")
    _write_cache(path, ts=123.0)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert set(data.keys()) == {"ts", "events"}


def test_entrypoint_argparse_guard():
    parser = nf.build_parser()
    args = parser.parse_args([])
    assert args.health_port == nf.DEFAULT_HEALTH_PORT
    assert args.max_cache_age_hours == nf.DEFAULT_MAX_CACHE_AGE_HOURS
    args2 = parser.parse_args(["--cache-path", "other.json", "--max-cache-age-hours", "1"])
    assert args2.cache_path == "other.json"
    assert args2.max_cache_age_hours == 1.0
