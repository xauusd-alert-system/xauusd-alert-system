"""ТЗ 10.1 — Bearer-token authentication for the API surface.

Covers: auth_required_when_configured, health_stays_open,
wrong_token_401, no_auth_mode_allows (backward compatibility),
require_auth_without_token_fails_startup, ingest rate limiting.
"""
import pytest
from fastapi.testclient import TestClient

import realtime.app as app_mod
from realtime.app import (
    IngestRateLimiter,
    resolve_api_auth_settings,
    validate_api_auth_startup,
)


@pytest.fixture()
def client():
    return TestClient(app_mod.app)


def _set_auth(monkeypatch, token="secret-token", require="1"):
    monkeypatch.setenv("API_AUTH_TOKEN", token)
    monkeypatch.setenv("API_REQUIRE_AUTH", require)


def test_auth_required_when_configured(client, monkeypatch):
    _set_auth(monkeypatch)
    res = client.get("/api/status")
    assert res.status_code == 401
    # with the correct bearer the same endpoint is reachable
    res = client.get("/api/status", headers={"Authorization": "Bearer secret-token"})
    assert res.status_code == 200


def test_health_stays_open(client, monkeypatch):
    """/health is public for load balancers — never authenticated."""
    _set_auth(monkeypatch)
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_wrong_token_401(client, monkeypatch):
    _set_auth(monkeypatch)
    res = client.get("/api/status", headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401
    res = client.get("/api/status", headers={"Authorization": "secret-token"})
    assert res.status_code == 401  # malformed header is not a bearer


def test_no_auth_mode_allows(client, monkeypatch):
    """Backward compatibility: require_auth=false (default) serves open."""
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("API_REQUIRE_AUTH", "0")
    res = client.get("/api/status")
    assert res.status_code == 200


def test_require_auth_without_token_fails_startup(monkeypatch):
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="API_AUTH_TOKEN"):
        validate_api_auth_startup(True, None)
    # config path: require_auth resolved true, token missing -> same failure
    monkeypatch.setenv("API_REQUIRE_AUTH", "1")
    require, token = resolve_api_auth_settings({"security": {"api": {"require_auth": True}}})
    assert require is True and token is None
    with pytest.raises(RuntimeError, match="API_AUTH_TOKEN"):
        validate_api_auth_startup(require, token)


def test_ledger_ingest_keeps_own_auth(client, monkeypatch, tmp_path):
    """Ingest is self-authenticated (bearer + HMAC); global bearer must not
    break or replace it."""
    _set_auth(monkeypatch, token="secret-token")
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.delenv("LEDGER_INGEST_SECRET", raising=False)
    res = client.post("/api/ledger/ingest", json={})
    # 503 = the endpoint's own fail-closed signing policy, NOT the global 401
    assert res.status_code == 503


def test_ingest_rate_limiter_allows_burst_then_429():
    limiter = IngestRateLimiter(rate_per_sec=0.0 + 0.001, burst=3)
    allowed = sum(limiter.allow("1.2.3.4") for _ in range(10))
    assert allowed == 3  # burst exhausted, refill negligible
    # a different IP has its own bucket
    assert limiter.allow("5.6.7.8") is True


def test_ingest_rate_limit_returns_429(client, monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setattr(
        app_mod, "_INGEST_LIMITER", IngestRateLimiter(rate_per_sec=0.001, burst=1)
    )
    codes = [client.post("/api/ledger/ingest", json={}).status_code for _ in range(3)]
    assert 429 in codes
