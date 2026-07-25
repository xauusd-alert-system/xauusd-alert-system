"""
Tests for data/ingestion.py rate-limit backoff and backfill_historical() pagination.
All HTTP calls are mocked with time.sleep patched out - no real network calls,
no real waiting.
Run with: pytest data/tests/test_backfill.py -v
"""
import os
import sys
from unittest.mock import patch, MagicMock
import pytest
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import backfill_historical, fetch_live_candles, _request_with_backoff, TWELVE_DATA_MAX_OUTPUTSIZE

CFG = load_config()
SESSIONS = CFG["sessions"]


def _make_values(start_dt: pd.Timestamp, n: int, step_minutes: int = 15):
    """Generates n descending timestamps (newest first, as Twelve Data returns)."""
    values = []
    for i in range(n):
        dt = start_dt - pd.Timedelta(minutes=step_minutes * i)
        values.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "open": "2400.0", "high": "2401.0", "low": "2399.0", "close": "2400.5", "volume": "100.0",
        })
    return values


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key"})
@patch("time.sleep", return_value=None)
@patch("data.ingestion.requests.get")
def test_request_with_backoff_retries_on_429_status_code(mock_get, mock_sleep):
    resp_429 = MagicMock(status_code=429)
    resp_429.json.return_value = {}
    resp_ok = MagicMock(status_code=200)
    resp_ok.json.return_value = {"values": []}
    resp_ok.raise_for_status = MagicMock()
    mock_get.side_effect = [resp_429, resp_ok]

    result = _request_with_backoff("http://fake.url", {"apikey": "x"})
    assert result == {"values": []}
    assert mock_get.call_count == 2
    assert mock_sleep.called


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key"})
@patch("time.sleep", return_value=None)
@patch("data.ingestion.requests.get")
def test_request_with_backoff_retries_on_payload_code_429(mock_get, mock_sleep):
    resp_429_payload = MagicMock(status_code=200)
    resp_429_payload.json.return_value = {"code": 429, "message": "rate limit"}
    resp_ok = MagicMock(status_code=200)
    resp_ok.json.return_value = {"values": []}
    resp_ok.raise_for_status = MagicMock()
    mock_get.side_effect = [resp_429_payload, resp_ok]

    result = _request_with_backoff("http://fake.url", {"apikey": "x"})
    assert result == {"values": []}
    assert mock_get.call_count == 2


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key"})
@patch("time.sleep", return_value=None)
@patch("data.ingestion.requests.get")
def test_request_with_backoff_raises_after_max_retries(mock_get, mock_sleep):
    resp_429 = MagicMock(status_code=429)
    resp_429.json.return_value = {}
    mock_get.return_value = resp_429

    with pytest.raises(RuntimeError):
        _request_with_backoff("http://fake.url", {"apikey": "x"}, max_retries=3)
    assert mock_get.call_count == 3


def test_fetch_live_candles_rejects_n_candles_over_max_outputsize():
    with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key"}):
        with pytest.raises(ValueError, match="exceeds Twelve Data"):
            fetch_live_candles("M15", TWELVE_DATA_MAX_OUTPUTSIZE + 1, SESSIONS)


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key"})
@patch("time.sleep", return_value=None)
@patch("data.ingestion.requests.get")
def test_backfill_historical_paginates_across_multiple_calls(mock_get, mock_sleep):
    """
    Simulate a range requiring 2 pages: first call returns 5000 rows ending at some
    cursor, second call returns the remaining rows and then empty to stop.
    """
    end_dt = pd.Timestamp("2026-01-10 00:00:00", tz="UTC")
    page1_values = _make_values(end_dt, 100, step_minutes=15)  # newest first
    page2_values = _make_values(page1_values and pd.Timestamp(page1_values[-1]["datetime"], tz="UTC") - pd.Timedelta(minutes=15), 50, step_minutes=15)

    resp1 = MagicMock(status_code=200)
    resp1.json.return_value = {"values": page1_values}
    resp1.raise_for_status = MagicMock()

    resp2 = MagicMock(status_code=200)
    resp2.json.return_value = {"values": page2_values}
    resp2.raise_for_status = MagicMock()

    resp_empty = MagicMock(status_code=200)
    resp_empty.json.return_value = {"values": []}
    resp_empty.raise_for_status = MagicMock()

    mock_get.side_effect = [resp1, resp2, resp_empty]

    start_date = (end_dt - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")

    df = backfill_historical("M15", start_date, end_date, SESSIONS)
    assert len(df) > 0
    assert df["timestamp_utc"].is_monotonic_increasing
    assert not df["timestamp_utc"].duplicated().any()
    assert mock_get.call_count >= 2


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key"})
@patch("time.sleep", return_value=None)
@patch("data.ingestion.requests.get")
def test_backfill_historical_raises_on_zero_rows(mock_get, mock_sleep):
    resp_empty = MagicMock(status_code=200)
    resp_empty.json.return_value = {"values": []}
    resp_empty.raise_for_status = MagicMock()
    mock_get.return_value = resp_empty

    with pytest.raises(RuntimeError):
        backfill_historical("M15", "2026-01-01", "2026-01-05", SESSIONS)


@patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "fake_key"})
@patch("time.sleep", return_value=None)
@patch("data.ingestion.requests.get")
def test_backfill_historical_deduplicates_overlapping_timestamps(mock_get, mock_sleep):
    end_dt = pd.Timestamp("2026-01-10 00:00:00", tz="UTC")
    overlapping_values = _make_values(end_dt, 20, step_minutes=15)

    resp1 = MagicMock(status_code=200)
    resp1.json.return_value = {"values": overlapping_values}
    resp1.raise_for_status = MagicMock()

    resp_empty = MagicMock(status_code=200)
    resp_empty.json.return_value = {"values": []}
    resp_empty.raise_for_status = MagicMock()

    mock_get.side_effect = [resp1, resp_empty]

    start_date = (end_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    df = backfill_historical("M15", start_date, end_dt.strftime("%Y-%m-%d"), SESSIONS)
    assert not df["timestamp_utc"].duplicated().any()
