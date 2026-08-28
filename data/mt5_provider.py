from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

import pandas as pd

from mt5_adapter.lazy import get_mt5_module

# ТЗ 8.6: the raw module handle is resolved through the adapter (no direct
# `import MetaTrader5` here). Module-level attribute access in tests
# (monkeypatch.setattr(mt5_provider.mt5, ...)) keeps working.
mt5 = get_mt5_module()


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

def _normalize_rates(rates, server_offset_hours: float = 0.0) -> pd.DataFrame:
    if rates is None or len(rates) == 0:
        raise MT5ProviderError(f"MT5 returned no candle data: {mt5.last_error()}")

    df = pd.DataFrame(rates)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})

    # N10 (audit 2026-08-10): MT5 bar timestamps are in the BROKER-SERVER timezone,
    # not UTC. The code previously declared them UTC with no offset, so session
    # tagging and the range tail-cut were silently shifted (e.g. EET/EEST = UTC+2/+3)
    # and the offset even "floats" across EU/US DST transitions. When a non-zero
    # server_time_offset_hours is configured, shift the raw server timestamps to
    # true UTC before they are used for session tagging / labeling / range slicing.
    if server_offset_hours:
        df["timestamp"] = df["timestamp"] - pd.Timedelta(hours=float(server_offset_hours))

    # N10: preserve `spread` and `real_volume` (broker-reported) when present in
    # the MT5 array. These were previously dropped, even though the actual broker
    # spread is exactly what an honest cost model (W1) needs. Optional columns do
    # not break the required-column contract below.
    if "spread" in df.columns:
        df["spread"] = pd.to_numeric(df["spread"], errors="coerce")
    if "real_volume" in df.columns:
        df["real_volume"] = pd.to_numeric(df["real_volume"], errors="coerce")

    required = ["timestamp", "open", "high", "low", "close", "volume"]
    df = df[required + [c for c in ("spread", "real_volume") if c in df.columns]].copy()

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
    server_offset_hours: float = 0.0,
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

    return _normalize_rates(rates, server_offset_hours=server_offset_hours)


def fetch_candles_range(
    symbol: str,
    timeframe: str,
    start_utc: datetime,
    end_utc: datetime,
    server_offset_hours: float = 0.0,
) -> pd.DataFrame:
    """Fetch MT5 bars in a UTC range, excluding a currently forming bar.

    server_offset_hours: broker-server timezone offset (hours) added to the UTC
    range bounds when talking to MT5, which treats datetimes as SERVER time
    (N10). The returned bars are normalized to true UTC.
    """
    if timeframe not in _TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("start_utc and end_utc must be timezone-aware")

    if end_utc <= start_utc:
        raise ValueError("end_utc must be later than start_utc")

    validate_symbol(symbol)

    start_utc = start_utc.astimezone(timezone.utc)
    end_utc = end_utc.astimezone(timezone.utc)

    # N10: MT5's copy_rates_range interprets the datetimes as SERVER time, so the
    # requested UTC bounds must be shifted by the server offset to select the same
    # wall-clock window.
    if server_offset_hours:
        start_server = start_utc + pd.Timedelta(hours=float(server_offset_hours))
        end_server = end_utc + pd.Timedelta(hours=float(server_offset_hours))
    else:
        start_server = start_utc
        end_server = end_utc

    rates = mt5.copy_rates_range(
        symbol,
        _TIMEFRAMES[timeframe],
        start_server,
        end_server,
    )

    df = _normalize_rates(rates, server_offset_hours=server_offset_hours)

    now_utc = pd.Timestamp.now(tz="UTC")
    tf_minutes = {
        "M1": 1, "M5": 5, "M15": 15, "M30": 30,
        "H1": 60, "H4": 240, "D1": 1440,
    }[timeframe]

    current_bar_open = now_utc.floor(f"{tf_minutes}min")
    return df.loc[df["timestamp"] < current_bar_open].reset_index(drop=True)
