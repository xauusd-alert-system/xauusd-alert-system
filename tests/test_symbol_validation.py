"""Tests for ticker symbol validation (P1-4)."""
import pytest

from usstocks.models import PremarketSnapshot, TradeSignal, validate_symbol


def test_validate_symbol_valid_cases():
    assert validate_symbol("aapl") == "AAPL"
    assert validate_symbol("TSLA ") == "TSLA"
    assert validate_symbol("BRK.B") == "BRK.B"
    assert validate_symbol("BF-B") == "BF-B"


def test_validate_symbol_invalid_cases():
    with pytest.raises(ValueError, match="Invalid symbol"):
        validate_symbol("")
    with pytest.raises(ValueError, match="Invalid symbol"):
        validate_symbol("TOOLONGSYMBOL")
    with pytest.raises(ValueError, match="Invalid symbol"):
        validate_symbol("AAPL$123")
    with pytest.raises(ValueError, match="Symbol must be string"):
        validate_symbol(12345)  # type: ignore


def test_models_validate_symbol_on_init():
    snap = PremarketSnapshot(
        symbol="nvda",
        price=120.0,
        prev_close=118.0,
        gap_pct=1.69,
        relative_volume=2.0,
        avg_daily_dollar_volume=100_000_000,
        spread_pct=0.01,
    )
    assert snap.symbol == "NVDA"

    with pytest.raises(ValueError):
        PremarketSnapshot(
            symbol="INVALID$$$",
            price=120.0,
            prev_close=118.0,
            gap_pct=1.69,
            relative_volume=2.0,
            avg_daily_dollar_volume=100_000_000,
            spread_pct=0.01,
        )
