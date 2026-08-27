"""Health server base: ok/degraded aggregation via TestClient."""
from __future__ import annotations

from fastapi.testclient import TestClient

from services.base import build_check, create_health_app, run_checks


def test_health_status_ok_when_all_checks_pass():
    checks = {
        "alpha": lambda: (True, "fine"),
        "beta": lambda: (True, "also fine"),
    }
    client = TestClient(create_health_app(checks))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["alpha"] == {"ok": True, "detail": "fine"}
    assert body["checks"]["beta"]["ok"] is True


def test_health_status_degraded_when_one_check_fails():
    checks = {
        "good": lambda: (True, "fine"),
        "bad": lambda: (False, "watermark stale"),
    }
    client = TestClient(create_health_app(checks))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["bad"] == {"ok": False, "detail": "watermark stale"}
    assert body["checks"]["good"]["ok"] is True


def test_raising_check_becomes_degraded_not_500():
    def boom():
        raise RuntimeError("exploded")

    client = TestClient(create_health_app({"boom": boom}))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert "exploded" in body["checks"]["boom"]["detail"]


def test_run_checks_and_build_check_helper():
    checks = build_check("only", lambda: (True, "ok"))
    assert run_checks(checks) == {
        "status": "ok",
        "checks": {"only": {"ok": True, "detail": "ok"}},
    }
