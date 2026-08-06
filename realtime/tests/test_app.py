"""
Tests for FastAPI Realtime Application, Dashboard API, Charts, Sentiment, and Monte Carlo.
"""
from fastapi.testclient import TestClient
import pytest

from realtime.app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_dashboard_endpoint(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "xauusd-alert-system" in res.text
    assert "<html" in res.text


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    json = res.json()
    assert json["status"] == "ok"
    assert "data_mode" in json


def test_api_status_endpoint(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    json = res.json()
    assert json["status"] == "online"
    assert "balance" in json


def test_api_matrix_endpoint(client):
    res = client.get("/api/matrix")
    assert res.status_code == 200
    json = res.json()
    assert "signals" in json
    assert len(json["signals"]) == 5


def test_api_correlation_endpoint(client):
    res = client.get("/api/correlation")
    assert res.status_code == 200
    json = res.json()
    assert len(json["assets"]) == 5
    assert len(json["matrix"]) == 5


def test_api_sentiment_endpoint(client):
    res = client.get("/api/sentiment")
    assert res.status_code == 200
    json = res.json()
    assert "score" in json
    assert "bias" in json
    assert "confidence" in json


def test_api_monte_carlo_endpoint(client):
    res = client.get("/api/monte-carlo")
    assert res.status_code == 200
    json = res.json()
    assert "var_95_usd" in json
    assert "profit_probability_pct" in json


def test_api_chart_endpoint(client):
    res = client.get("/api/chart/XAUUSD")
    assert res.status_code == 200
    assert "<svg" in res.text
    assert "XAUUSD" in res.text


def test_api_control_endpoints(client):
    # Pause
    res_pause = client.post("/api/control/pause")
    assert res_pause.status_code == 200
    assert "приостановлена" in res_pause.json()["message"]

    # Status reflects paused
    res_status = client.get("/api/status")
    assert res_status.json()["trading_paused"] is True

    # Resume
    res_resume = client.post("/api/control/resume")
    assert res_resume.status_code == 200
    assert "возобновлена" in res_resume.json()["message"]

    # Close all
    res_close = client.post("/api/control/closeall")
    assert res_close.status_code == 200
    assert "закрыты" in res_close.json()["message"]
