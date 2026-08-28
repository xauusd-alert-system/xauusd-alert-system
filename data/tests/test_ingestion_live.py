"""
Tests for data/ingestion.py::fetch_live_candles - HTTP calls are fully mocked,
no real network requests are made. Validates response parsing, error handling,
and the unified fetch_candles() dispatcher.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import fetch_candles, fetch_live_candles

CFG = load_config()
SESSIONS = CFG["sessions"]


def _mock_twelvedata_payload(n=5):
    values = []
    base_ts = 1700000000
    for i in range(n):
        ts = base_ts + i * 900
        dt = pd.Timestamp(ts, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
        values.append({
            "datetime": dt, "open": "2400.5", "high": "2402.0",
            "low": "2399.0", "close": "2401.0", "volume": "150.0",
        })
    return {"values": values}


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key_for_testing"})
@patch("data.ingestion.requests.get")
def test_fetch_live_candles_parses_valid_response(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = _mock_twelvedata_payload(5)
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    df = fetch_live_candles("M15", 5, SESSIONS)
    assert len(df) == 5
    assert list(df.columns[:6]) == ["timestamp_utc", "open", "high", "low", "close", "volume"]
    assert df["timestamp_utc"].is_monotonic_increasing
    assert "session" in df.columns
    assert mock_get.called


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key_for_testing"})
@patch("data.ingestion.requests.get")
def test_fetch_live_candles_raises_on_malformed_response(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"error": "invalid symbol"}
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    with pytest.raises(RuntimeError):
        fetch_live_candles("M15", 5, SESSIONS)


def test_fetch_live_candles_raises_without_api_key():
    """Must fail loudly if API key env var is missing - never silently fall back to mock."""
    env_backup = os.environ.pop("TWELVE_DATA_API_KEY", None)
    try:
        with pytest.raises(EnvironmentError):
            fetch_live_candles("M15", 5, SESSIONS)
    finally:
        if env_backup is not None:
            os.environ["TWELVE_DATA_API_KEY"] = env_backup


def test_fetch_live_candles_rejects_unsupported_timeframe():
    with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key"}):
        with pytest.raises(ValueError):
            fetch_live_candles("M30", 5, SESSIONS)


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key_for_testing"})
@patch("data.ingestion.requests.get")
def test_fetch_candles_dispatcher_routes_to_live(mock_get):
    mock_resp = MagicMock()
    mock_resp.json.return_value = _mock_twelvedata_payload(3)
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    df = fetch_candles("M15", 3, SESSIONS, mode="live")
    assert len(df) == 3
    assert mock_get.called


def test_fetch_candles_dispatcher_routes_to_mock():
    df = fetch_candles("M15", 10, SESSIONS, mode="mock")
    assert len(df) == 10


def test_fetch_candles_dispatcher_rejects_unknown_mode():
    with pytest.raises(ValueError):
        fetch_candles("M15", 10, SESSIONS, mode="bogus")


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key_for_testing"})
@patch("data.ingestion.requests.get")
def test_fetch_live_candles_raises_on_http_error(mock_get):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("503 Service Unavailable")
    mock_get.return_value = mock_resp

    with pytest.raises(Exception):
        fetch_live_candles("M15", 5, SESSIONS)
