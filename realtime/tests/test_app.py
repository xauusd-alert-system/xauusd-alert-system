"""
Tests for FastAPI Realtime Application, Dashboard API, Charts, Sentiment, and Monte Carlo.
"""

import pytest
from fastapi.testclient import TestClient

from realtime.app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_dashboard_endpoint(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "xauusd-alert-system" in res.text
    assert "<html" in res.text


def test_dashboard_has_no_hardcoded_live_metrics_or_duplicate_js_declaration(client):
    text = client.get("/").text
    for fake in ("$100,000.00", "BULLISH (+0.65)", "-$240.50", "+safe haven"):
        assert fake not in text
    assert text.count('let bg = "bg-slate-800/30 text-slate-400";') == 1
    assert "sent.available" in text and "mc.available" in text


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
    assert {"source", "mode", "as_of_utc", "available"} <= set(json)
    if not json["available"]:
        assert json["balance"] is None  # never fabricate a $100k account


def test_api_paper_status_never_exposes_outcome_metrics(client):
    res = client.get("/api/paper-status")
    assert res.status_code == 200
    payload = res.json()
    assert {"available", "source", "mode", "as_of_utc"} <= set(payload)
    assert not ({"pnl", "profit_factor", "win_rate"} & set(payload))


def test_api_matrix_endpoint(client):
    res = client.get("/api/matrix")
    assert res.status_code == 200
    json = res.json()
    assert "signals" in json
    assert len(json["signals"]) == 5
    assert {"source", "mode", "as_of_utc"} <= set(json)
    assert all({"source", "mode", "as_of_utc", "available"} <= set(s) for s in json["signals"])


def test_api_correlation_endpoint_has_no_static_demo_fallback(client, monkeypatch):
    monkeypatch.setattr("realtime.app.DATA_MODE", "mock")
    res = client.get("/api/correlation")
    assert res.status_code == 200
    payload = res.json()
    assert payload["available"] is False
    assert payload["assets"] == [] and payload["matrix"] == []
    assert payload["source"] == "unavailable"


def test_api_sentiment_endpoint_does_not_publish_sample_headlines(client, monkeypatch):
    def _fail(*args, **kwargs):
        raise RuntimeError("feed down")

    monkeypatch.setattr("data.news_filter.fetch_economic_calendar", _fail)
    monkeypatch.setattr(
        "data.news_filter.news_feed_status",
        lambda: {"available": False, "error": "feed down", "event_count": 0},
    )
    payload = client.get("/api/sentiment").json()
    assert payload["available"] is False
    assert payload["score"] is None and payload["bias"] is None
    assert payload["matched_terms"] == []
    assert payload["reason"] == "news_feed_unavailable"


def test_api_sentiment_computes_aggregate_from_real_feed(client, monkeypatch):
    events = [
        {
            "title": "Fed Rate Cut Expectations Rise",
            "country": "USD",
            "timestamp_utc": 1_800_000_000,
            "datetime_str": "2027-01-01 12:00 UTC",
        },
        {
            "title": "Strong Jobs Report Beats",
            "country": "USD",
            "timestamp_utc": 1_800_008_640,
            "datetime_str": "2027-01-02 12:00 UTC",
        },
        {
            "title": "Geopolitical Tensions Escalate",
            "country": "USD",
            "timestamp_utc": 1_800_017_280,
            "datetime_str": "2027-01-03 12:00 UTC",
        },
    ]
    monkeypatch.setattr("data.news_filter.fetch_economic_calendar", lambda: events)
    monkeypatch.setattr(
        "data.news_filter.news_feed_status",
        lambda: {"available": True, "last_success_age_seconds": 120, "error": None, "event_count": 3},
    )
    payload = client.get("/api/sentiment").json()
    assert payload["available"] is True
    assert payload["bias"] in {"bullish", "bearish", "neutral"}
    assert payload["score"] is not None and payload["confidence"] is not None
    assert len(payload["events"]) == 3
    assert payload["events"][0]["title"] == "Fed Rate Cut Expectations Rise"
    assert payload["events"][0]["bias"] == "bullish"
    assert payload["events"][1]["bias"] == "bearish"
    assert payload["feed"]["available"] is True
    assert payload["source"] == "forexfactory_economic_calendar"
    assert payload["freshness_status"] == "fresh"


def test_api_sentiment_available_empty_calendar_is_not_a_fallback(client, monkeypatch):
    monkeypatch.setattr("data.news_filter.fetch_economic_calendar", lambda: [])
    monkeypatch.setattr(
        "data.news_filter.news_feed_status",
        lambda: {"available": True, "last_success_age_seconds": 60, "error": None, "event_count": 0},
    )
    payload = client.get("/api/sentiment").json()
    assert payload["available"] is True
    assert payload["reason"] == "calendar_empty"
    assert payload["score"] == 0.0 and payload["bias"] == "neutral"
    assert payload["events"] == []


def test_api_monte_carlo_endpoint_has_no_hypothetical_fallback(client, monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "missing.sqlite"))
    payload = client.get("/api/monte-carlo").json()
    assert payload["available"] is False
    assert "var_95_usd" not in payload


def test_api_monte_carlo_uses_primary_event_ledger(client, monkeypatch, tmp_path):
    from data.trading_event_ledger import append_trading_event

    db = str(tmp_path / "trades.sqlite")
    for ticket, pnl in ((1, 10.0), (2, -4.0)):
        append_trading_event(
            db,
            event_type="position_closed",
            signal_id=f"s{ticket}",
            asset_key="XAUUSD",
            strategy_version="v3",
            config_hash="cfg",
            actor="broker_history",
            position_ticket=ticket,
            payload={"realized_pnl": pnl},
        )
    monkeypatch.setenv("TRADE_LOG_DB_PATH", db)
    payload = client.get("/api/monte-carlo").json()
    assert payload["available"] is True
    assert payload["source"] == "trading_events.position_closed.realized_pnl"
    assert payload["n_trades"] == 2


def test_api_chart_endpoint_has_no_random_fallback(client, monkeypatch):
    monkeypatch.setattr("realtime.app.DATA_MODE", "mock")
    res = client.get("/api/chart/XAUUSD")
    assert res.status_code == 503
    assert res.json()["detail"]["available"] is False


def test_institutional_metrics_have_no_static_fallback(client, monkeypatch):
    monkeypatch.setattr("realtime.app.DATA_MODE", "mock")
    payload = client.get("/api/institutional-metrics").json()
    assert payload["available"] is False
    assert payload["metrics"] == {}


def test_api_control_endpoints_are_disabled(client, monkeypatch):
    """Web-UI spec §11/§12: ALL browser mutation controls are off until a real
    command bus exists (idempotency, confirmation, kill-switch, reconciliation).
    """
    monkeypatch.setenv("DASHBOARD_CONTROL_TOKEN", "test-control-token")
    headers = {"Authorization": "Bearer test-control-token"}
    # unauthenticated -> 403
    assert client.post("/api/control/pause").status_code == 403
    assert client.post("/api/control/closeall").status_code == 403
    # authenticated but disabled -> 501 for every action
    for action in ("pause", "resume", "closeall", "anything"):
        res = client.post(f"/api/control/{action}", headers=headers)
        assert res.status_code == 501
        assert "disabled" in res.json()["detail"]
    # the in-memory pause flag is never flipped through the web process
    assert client.get("/api/status").json()["trading_paused"] is False
