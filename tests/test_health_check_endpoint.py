"""Tests for Health check API endpoints (P1-10)."""
import pytest
from fastapi.testclient import TestClient

from usstocks.health_server import create_health_app
from usstocks.models import RiskState


def test_health_endpoint_healthy_state():
    state = RiskState(
        session_date="2026-08-27",
        realized_pnl_usd=12.5,
        trades_taken=1,
        active_symbol=None,
        day_stopped=False,
    )
    app = create_health_app(state=state, start_time=1000.0)
    client = TestClient(app)

    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["healthy"] is True
    assert data["realized_pnl_usd"] == 12.5
    assert data["trades_taken"] == 1
    assert data["session_date"] == "2026-08-27"


def test_status_and_metrics_endpoints():
    state = RiskState(
        session_date="2026-08-27",
        realized_pnl_usd=-10.0,
        trades_taken=1,
        consecutive_losses=1,
    )
    app = create_health_app(state=state, start_time=1000.0)
    client = TestClient(app)

    resp_status = client.get("/api/status")
    assert resp_status.status_code == 200

    resp_metrics = client.get("/api/metrics")
    assert resp_metrics.status_code == 200
    metrics = resp_metrics.json()
    assert metrics["realized_pnl_usd"] == -10.0
    assert metrics["trades_taken"] == 1
    assert metrics["consecutive_losses"] == 1
