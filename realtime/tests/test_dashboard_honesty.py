"""Honesty/no-fallback tests for the legacy dashboard API (web-UI spec §12).

The rule under test: an unavailable source must NEVER become a numeric
fallback — no $100,000 balance, no neutral confidence=0.50, no fabricated
signal row, no random chart.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import realtime.app as app_module
from realtime.app import app


@pytest.fixture
def client():
    return TestClient(app)


class _FakeAccount:
    balance = 12345.67
    equity = 12222.22
    login = 999
    trade_mode = 1


class _FakeMT5:
    def account_info(self):
        return _FakeAccount()

    def positions_get(self):
        return []


class _FailingPipeline:
    """RealtimePipeline stand-in that fails for every asset."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("boom: model unavailable")

    def generate_signal(self, n_candles=300):
        raise RuntimeError("boom: model unavailable")


def test_matrix_error_is_explicit_state_not_neutral_fallback(client, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_MODE", "live")
    monkeypatch.setattr(app_module, "RealtimePipeline", _FailingPipeline)
    res = client.get("/api/matrix")
    assert res.status_code == 200
    payload = res.json()
    assert len(payload["signals"]) == 5
    for sig in payload["signals"]:
        assert sig["available"] is False
        assert sig["status"] == "error"
        assert sig["bias"] is None
        assert sig["confidence"] is None
        assert sig["reason"] == "boom: model unavailable"
    # no fabricated neutral row anywhere
    blob = str(payload)
    assert "neutral" not in blob
    assert "0.50" not in blob and "0.5" not in blob


def test_matrix_non_live_has_no_confidence(client, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_MODE", "mock")
    payload = client.get("/api/matrix").json()
    assert payload["source"] == "unavailable"
    for sig in payload["signals"]:
        assert sig["available"] is False
        assert sig["status"] == "unavailable"
        assert sig["bias"] is None and sig["confidence"] is None


def test_status_never_falls_back_to_balance(client, monkeypatch):
    monkeypatch.setattr(app_module.sc, "ensure_mt5_connection", lambda: False)
    payload = client.get("/api/status").json()
    assert payload["available"] is False
    assert payload["balance"] is None
    assert payload["equity"] is None
    assert payload["floating_pnl"] is None
    assert payload["freshness_status"] == "offline"
    assert payload["balance"] != 100000.0


def test_status_fresh_when_mt5_available(client, monkeypatch):
    monkeypatch.setattr(app_module.sc, "ensure_mt5_connection", lambda: True)
    monkeypatch.setattr(app_module.sc, "get_mt5", lambda: _FakeMT5())
    payload = client.get("/api/status").json()
    assert payload["available"] is True
    assert payload["balance"] == 12345.67
    assert payload["freshness_status"] == "fresh"
    assert payload["source"] == "mt5_account"


def test_positions_offline_without_mt5(client, monkeypatch):
    monkeypatch.setattr(app_module.sc, "ensure_mt5_connection", lambda: False)
    payload = client.get("/api/positions").json()
    assert payload["available"] is False
    assert payload["positions"] == []
    assert payload["freshness_status"] == "offline"  # producer unreachable
    assert payload["source"] == "unavailable"


def test_correlation_sentiment_include_freshness(client, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_MODE", "mock")

    def _fail(*args, **kwargs):
        raise RuntimeError("feed offline")

    monkeypatch.setattr("data.news_filter.fetch_economic_calendar", _fail)
    monkeypatch.setattr(
        "data.news_filter.news_feed_status",
        lambda: {"available": False, "error": "feed offline", "event_count": 0},
    )
    corr = client.get("/api/correlation").json()
    assert corr["freshness_status"] == "offline"  # MT5 producer not reachable
    assert corr["as_of_utc_ms"] is None
    sent = client.get("/api/sentiment").json()
    assert sent["freshness_status"] == "waiting"
    assert sent["score"] is None


def test_monte_carlo_missing_ledger_is_not_a_fallback(client, monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "missing.sqlite"))
    payload = client.get("/api/monte-carlo").json()
    assert payload["available"] is False
    assert "var_95_usd" not in payload
    assert payload["freshness_status"] in {"waiting", "offline"}


# ---------------------------------------------------------------------------
# /api/ml-prob — raw ML P(long)/P(short) panel (2026-08-25).
# Same honesty contract as /api/matrix: unavailable/error must be explicit,
# never fabricated neutral probabilities that look model-computed.
# ---------------------------------------------------------------------------

class _FakeMLProbPipeline:
    """RealtimePipeline stand-in producing a small frame + fixed probabilities."""

    timeframe = "M15"
    effective_cfg = {
        "ensemble": {
            "min_ml_probability": 0.55,
            "ml_confidence_floor": 0.62,
            "min_confidence_to_alert": 0.66,
        }
    }

    def __init__(self, *args, **kwargs):
        self._predictor = _FakePredictor()

    def get_frame(self, n_candles=300, build_features=True):
        import pandas as pd
        n = 130
        return pd.DataFrame({
            "timestamp_utc": [1_000_000 + i * 900 for i in range(n)],
            "close": [4600.0 + i * 0.1 for i in range(n)],
            "regime": ["range"] * n,
            "session": ["newyork"] * n,
            "feat_a": [0.5] * n,
        })


class _FakePredictor:
    feature_cols = ["feat_a"]

    def predict_proba(self, df):
        import pandas as pd
        n = len(df)
        return pd.DataFrame({
            "p_long": [0.6] * n,
            "p_short": [0.4] * n,
        }, index=df.index)


def test_mlprob_non_live_is_explicit_unavailable(client, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_MODE", "mock")
    payload = client.get("/api/ml-prob").json()
    assert payload["available"] is False
    assert payload["status"] == "unavailable"
    assert "mock" in payload["reason"]
    assert "history" not in payload
    blob = str(payload)
    assert "0.5" not in blob


def test_mlprob_error_is_explicit_not_neutral(client, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_MODE", "live")
    monkeypatch.setattr(app_module, "RealtimePipeline", _FailingPipeline)
    payload = client.get("/api/ml-prob").json()
    assert payload["available"] is False
    assert payload["status"] == "error"
    assert payload["reason"] == "boom: model unavailable"
    assert "history" not in payload
    blob = str(payload)
    assert "0.5" not in blob


def test_mlprob_happy_path_returns_history_and_latest(client, monkeypatch):
    monkeypatch.setattr(app_module, "DATA_MODE", "live")
    monkeypatch.setattr(app_module, "RealtimePipeline", _FakeMLProbPipeline)
    payload = client.get("/api/ml-prob").json()
    assert payload["available"] is True
    assert payload["status"] == "ok"
    assert len(payload["history"]) == 120
    # chronological, per-bar probabilities present
    h = payload["history"]
    assert h[0]["ts"] < h[-1]["ts"]
    assert h[-1]["p_long"] == 0.6 and h[-1]["p_short"] == 0.4
    assert h[-1]["regime"] == "range" and h[-1]["session"] == "newyork"
    lat = payload["latest"]
    assert lat["ts"] == h[-1]["ts"]
    assert "ensemble_bias" in lat and "ensemble_confidence" in lat
    assert payload["thresholds"]["min_ml_probability"] == 0.55


def test_dashboard_html_marks_diagnostic_and_has_no_controls(client):
    text = client.get("/").text
    assert "INTERNAL DIAGNOSTIC VIEW" in text
    assert "не является live-терминалом" in text
    # dead control plumbing removed from the page
    assert "sendControl" not in text
    assert "/api/control/" not in text
    assert "closeall" not in text
