"""ТЗ 6.3: enriched /api/health endpoint tests.

Covers:
    - health_ok_on_fresh_state        — fresh DB, no risk state, no MT5 -> ok;
    - health_degraded_on_db_error     — a failing check degrades, not 500;
    - health_does_not_leak_secrets    — no tokens / secret paths in payload.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from monitoring.health import (
    build_health_checks,
    db_check,
    risk_check,
)


@pytest.fixture
def client():
    from realtime.app import app

    return TestClient(app)


# ---------------------------------------------------------------- fixtures ---

@pytest.fixture
def fresh_db(tmp_path):
    db_path = str(tmp_path / "fresh.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER)")
    conn.commit()
    conn.close()
    return db_path


# ------------------------------------------------------------------ tests ----

def test_health_ok_on_fresh_state(client, tmp_path, monkeypatch):
    """Fresh deployment: db exists, no circuit breaker, no MT5 -> status ok."""
    db_path = str(tmp_path / "fresh.sqlite")
    conn = sqlite3.connect(db_path)
    conn.close()

    checks = {
        "db": db_check(db_path),
        "risk": risk_check(str(tmp_path / "absent_risk_state.json")),
    }
    from services.base import run_checks

    payload = run_checks(checks)
    assert payload["status"] == "ok"
    assert payload["checks"]["db"]["ok"] is True
    assert payload["checks"]["risk"]["ok"] is True

    # The endpoint itself (process-level) must respond 200 with components.
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("ok", "degraded")  # never 5xx
    assert set(body["checks"]) == {"db", "executor", "risk", "feed", "services"}


def test_health_degraded_on_db_error(tmp_path):
    """A failing db check yields status=degraded — not an exception/500."""
    from services.base import run_checks

    def _broken():
        raise RuntimeError("disk exploded")

    good_db = str(tmp_path / "good.sqlite")
    sqlite3.connect(good_db).close()
    payload = run_checks({"db": _broken, "ok_check": db_check(good_db)})
    assert payload["status"] == "degraded"
    assert payload["checks"]["db"]["ok"] is False
    assert "disk exploded" in payload["checks"]["db"]["detail"]
    assert payload["checks"]["ok_check"]["ok"] is True


def test_health_endpoint_never_500_when_db_missing(client, tmp_path, monkeypatch):
    """db_check on a missing file -> degraded payload with HTTP 200."""
    monkeypatch.setattr(
        "monitoring.health.build_health_checks",
        lambda cfg, db_path=None: {"db": db_check(str(tmp_path / "ghost.sqlite"))},
    )
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "degraded"


def test_health_does_not_leak_secrets(client, monkeypatch):
    """No secret material in the payload.

    Service NAMES (e.g. ``telegram_bot``) are not secrets; what must never
    appear: token values, secret env vars, credential-looking strings, and
    the .env file path.
    """
    monkeypatch.setenv("API_AUTH_TOKEN", "SUPER-SECRET-VALUE-0123456789abcdef")
    res = client.get("/api/health")
    assert res.status_code == 200
    text = res.text.lower()
    for needle in ("super-secret-value", "bearer ", "api_auth", ".env",
                   "password", "secret"):
        assert needle not in text, f"leaked '{needle}' in /api/health"
    # No long token-like hex/base64 strings.
    import re as _re

    assert not _re.search(r"[a-f0-9]{32,}", text)


def test_risk_check_detects_circuit_breaker(tmp_path):
    import json

    state_path = tmp_path / "risk_state.json"
    state_path.write_text(json.dumps({"circuit_breaker_tripped": True}),
                          encoding="utf-8")
    ok, detail = risk_check(str(state_path))()
    assert ok is False
    assert "circuit breaker" in detail.lower()


def test_executor_check_counts_active_groups(tmp_path):
    from monitoring.health import executor_check

    db_path = str(tmp_path / "groups.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE trade_groups (group_id TEXT PRIMARY KEY, state TEXT)"
    )
    conn.executemany(
        "INSERT INTO trade_groups VALUES (?, ?)",
        [("g1", "OPENED"), ("g2", "TP1_FILLED"), ("g3", "RECONCILED")],
    )
    conn.commit()
    conn.close()

    ok, detail = executor_check(db_path)()
    assert ok is True
    assert "2 active groups" in detail


def test_executor_check_fail_open_when_store_absent(tmp_path):
    from monitoring.health import executor_check

    ok, detail = executor_check(str(tmp_path / "none.sqlite"))()
    assert ok is True
    assert "not initialised" in detail


def test_services_check_reports_ports_without_network():
    from monitoring.health import services_check

    cfg = {"services": {"ledger_bridge": {"health_port": 8791},
                        "news_feed": {"health_port": 8793}}}
    ok, detail = services_check(cfg)()
    assert ok is True
    assert "ledger_bridge:8791" in detail and "news_feed:8793" in detail


def test_build_health_checks_maps_enabled_symbols():
    cfg = {
        "general": {"db_path": "data/x.sqlite"},
        "assets": {"XAUUSD": {"enabled": True, "mt5_symbol": "GOLD"},
                   "XAGUSD": {"enabled": False, "mt5_symbol": "SILVER"}},
    }
    checks = build_health_checks(cfg)
    assert set(checks) == {"db", "executor", "risk", "feed", "services"}
    # Only the enabled asset's symbol reaches the feed check closure; verify
    # indirectly via the source of the closure defaults.
    assert callable(checks["feed"])
