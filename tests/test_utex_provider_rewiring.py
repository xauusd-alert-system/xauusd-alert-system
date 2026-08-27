"""Tests for UTEX provider rewiring (P0-1)."""
from unittest.mock import MagicMock, patch

from challenge.manual import alerter
from usstocks.data.utex_provider import UtexClient, decode_candles, fetch_candles, refresh_access


def test_alerter_uses_unified_utex_provider():
    mock_payload = {
        "candles": [
            {"time": 1700000000, "open": 10000000000, "high": 10500000000, "low": 9900000000, "close": 10200000000, "volume": 500}
        ]
    }
    decoded = decode_candles(mock_payload)
    assert len(decoded) == 1
    assert decoded[0]["open"] == 100.0
    assert decoded[0]["high"] == 105.0

    # Test alerter delegates
    with patch("usstocks.data.utex_provider.fetch_candles") as mock_fetch:
        mock_fetch.return_value = decoded
        res = alerter.fetch_candles("fake_token", 12345, 10)
        assert res == decoded
        mock_fetch.assert_called_once_with("fake_token", 12345, candles_count=10)
