"""Tests for UtexClient circuit breaker and caching (P2-7, P2-10)."""
import json
import tempfile
from unittest.mock import MagicMock
import pytest

from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, CircuitState
from usstocks.data.utex_provider import UtexClient


def test_utex_client_caches_access_token():
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        json.dump({"refresh_token": "valid_refresh_token"}, tf)
        tf_path = tf.name

    mock_post = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"accessToken": "access_jwt_123"}
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    client = UtexClient(token_file=tf_path, post=mock_post, token_ttl_seconds=300.0)

    # First call hits mock_post
    t1 = client.refresh_access()
    assert t1 == "access_jwt_123"
    assert mock_post.call_count == 1

    # Second call returns cached token without network call
    t2 = client.refresh_access()
    assert t2 == "access_jwt_123"
    assert mock_post.call_count == 1


def test_utex_client_circuit_breaker_blocks_repeated_failures():
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        json.dump({"refresh_token": "invalid_refresh_token"}, tf)
        tf_path = tf.name

    mock_post = MagicMock(side_effect=RuntimeError("500 Server Error"))
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
    client = UtexClient(token_file=tf_path, post=mock_post, circuit_breaker=cb)

    # Failure 1
    with pytest.raises(RuntimeError):
        client.refresh_access()

    # Failure 2 -> opens circuit
    with pytest.raises(RuntimeError):
        client.refresh_access()

    assert cb.state == CircuitState.OPEN

    # Subsequent call fails fast with CircuitBreakerOpenError without calling mock_post
    with pytest.raises(CircuitBreakerOpenError):
        client.refresh_access()
    assert mock_post.call_count == 2
