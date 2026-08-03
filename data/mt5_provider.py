from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

import MetaTrader5 as mt5
import pandas as pd


_TIMEFRAMES: Final[dict[str, int]] = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


class MT5ProviderError(RuntimeError):
    """Raised when FxPro MT5 data cannot be read safely."""


def initialize_mt5() -> None:
    if mt5.initialize():
        return
    raise MT5ProviderError(f"MT5 initialize failed: {mt5.last_error()}")


def shutdown_mt5() -> None:
    mt5.shutdown()


def validate_symbol(symbol: str) -> None:
    """Confirm an MT5 symbol exists and enable it in Market Watch."""
    initialize_mt5()

    info = mt5.symbol_info(symbol)
    if info is None:
        raise MT5ProviderError(
            f"MT5 symbol {symbol!r} was not found. "
            "Check the exact name in the FxPro MT5 Symbols window."
        )

    if not mt5.symbol_select(symbol, True):
        code, message = mt5.last_error()
        raise MT5ProviderError(
            f"Could not enable MT5 symbol {symbol!r} in Market Watch: "
            f"{code} {message}"
        )

def _normalize_rates(rates) -> pd.DataFrame:
    if rates is None or len(rates) == 0:
        raise MT5ProviderError(f"MT5 returned no candle data: {mt5.last_error()}")

    df = pd.DataFrame(rates)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})

    required = ["timestamp", "open", "high", "low", "close", "volume"]
    df = df[required].copy()

    for column in ("open", "high", "low", "close", "volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = (
        df.dropna(subset=required)
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    if df.empty:
        raise MT5ProviderError("MT5 returned candle rows, but none were valid")

    return df


def fetch_closed_candles(
    symbol: str,
    timeframe: str,
    count: int,
) -> pd.DataFrame:
    """Fetch only completed bars; MT5 position 0 is intentionally excluded."""
    if timeframe not in _TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    if count < 1:
        raise ValueError("count must be at least 1")

    validate_symbol(symbol)

    rates = mt5.copy_rates_from_pos(
        symbol,
        _TIMEFRAMES[timeframe],
        1,
        count,
    )

    return _normalize_rates(rates)


def fetch_candles_range(
    symbol: str,
    timeframe: str,
    start_utc: datetime,
    end_utc: datetime,
) -> pd.DataFrame:
    """Fetch MT5 bars in a UTC range, excluding a currently forming bar."""
    if timeframe not in _TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("start_utc and end_utc must be timezone-aware")

    if end_utc <= start_utc:
        raise ValueError("end_utc must be later than start_utc")

    validate_symbol(symbol)

    start_utc = start_utc.astimezone(timezone.utc)
    end_utc = end_utc.astimezone(timezone.utc)

    rates = mt5.copy_rates_range(
        symbol,
        _TIMEFRAMES[timeframe],
        start_utc,
        end_utc,
    )

    df = _normalize_rates(rates)

    now_utc = pd.Timestamp.now(tz="UTC")
    tf_minutes = {
        "M1": 1, "M5": 5, "M15": 15, "M30": 30,
        "H1": 60, "H4": 240, "D1": 1440,
    }[timeframe]

    current_bar_open = now_utc.floor(f"{tf_minutes}min")
    return df.loc[df["timestamp"] < current_bar_open].reset_index(drop=True)
