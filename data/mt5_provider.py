from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Final

import MetaTrader5 as mt5
import pandas as pd


logger = logging.getLogger(__name__)


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


def detect_server_offset_hours(
    symbol: str = "EURUSD",
    max_abs_offset_hours: float = 14.0,
    fallback: float = 0.0,
) -> float:
    """Measure the broker-server timezone offset from a live tick (float only).

    See ``detect_server_offset_hours_detailed`` for the full semantics and the
    reason why a fallback may be returned. This thin wrapper keeps callers
    that only need the number (and the provider's own log lines) unchanged.
    """
    offset, _ = detect_server_offset_hours_detailed(
        symbol=symbol, max_abs_offset_hours=max_abs_offset_hours, fallback=fallback,
    )
    return offset


def detect_server_offset_hours_detailed(
    symbol: str = "EURUSD",
    max_abs_offset_hours: float = 14.0,
    fallback: float = 0.0,
) -> tuple[float, dict]:
    """Measure the broker-server timezone offset from a live tick.

    MT5 bar/tick timestamps are in SERVER time (FxPro EET/EEST = UTC+2/+3, and
    the offset floats across EU/US DST transitions). The offset is measured as
    ``tick.time - time.time()`` rounded to the nearest whole hour — the same
    measurement that produced ``+3`` on 2026-08-25 (last tick 20:21 server vs
    17:21 real UTC, delta +180 min).

    The measurement is only trusted while the market is OPEN:
      * on Sat/Sun (UTC) the market is closed and the last tick is stale, so
        ``tick.time - now`` is downtime, not the offset — fallback is used;
      * a delta beyond ``max_abs_offset_hours`` (real timezones span UTC-12..+14)
        means the tick is days old (weekend/holiday) — fallback is used.
    In those cases — or when the terminal is unreachable — ``fallback`` is
    returned with a warning; the caller decides what the fallback should be
    (e.g. the config's last measured value, so a DST flip that happens during
    the closed weekend self-heals at the next open).

    Returns ``(offset, info)`` where ``info`` carries the provenance for log
    forensics: ``mode`` ("detected" | "fallback"), a human ``reason`` and the
    measured ``delta_hours`` when available.
    """
    try:
        initialize_mt5()
        if not mt5.symbol_select(symbol, True):
            raise MT5ProviderError(f"symbol_select failed for {symbol!r}")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5ProviderError(f"no tick available for {symbol!r}")
    except Exception as exc:  # terminal down / symbol missing
        logger.warning(
            "server-offset auto-detect unavailable (%s); using fallback %.1fh",
            exc, fallback,
        )
        return float(fallback), {
            "mode": "fallback", "reason": f"mt5_unavailable: {exc}",
            "delta_hours": None,
        }

    if _is_weekend_utc():
        logger.warning(
            "server-offset auto-detect: weekend, market closed; using fallback %.1fh",
            fallback,
        )
        return float(fallback), {
            "mode": "fallback", "reason": "weekend_market_closed",
            "delta_hours": None,
        }

    delta_sec = float(tick.time) - time.time()
    if abs(delta_sec) > max_abs_offset_hours * 3600:
        logger.warning(
            "server-offset auto-detect got implausible delta %.1fh (market closed?); "
            "using fallback %.1fh", delta_sec / 3600.0, fallback,
        )
        return float(fallback), {
            "mode": "fallback",
            "reason": f"implausible_delta_hours={delta_sec / 3600.0:.1f}",
            "delta_hours": delta_sec / 3600.0,
        }

    offset = float(round(delta_sec / 3600.0))
    logger.info(
        "server-offset auto-detect: %s tick delta %.1fs -> UTC%+dh",
        symbol, delta_sec, int(offset),
    )
    return offset, {
        "mode": "detected",
        "reason": f"tick_delta_hours={delta_sec / 3600.0:.4f}",
        "delta_hours": delta_sec / 3600.0,
    }


def _is_weekend_utc() -> bool:
    """True on Saturday/Sunday (UTC) — the market is closed and a live-tick
    offset measurement would be downtime, not the server offset."""
    return datetime.now(timezone.utc).weekday() >= 5


def resolve_server_offset(market_data: dict | None) -> float:
    """Resolve the broker-server UTC offset from config, with auto-detection.

    Thin wrapper over ``resolve_server_offset_detailed`` returning only the
    number (the provider's own log lines still carry the detail).
    """
    offset, _ = resolve_server_offset_detailed(market_data)
    return offset


def resolve_server_offset_detailed(market_data: dict | None) -> tuple[float, dict]:
    """Resolve the broker-server UTC offset from config, with provenance.

    * numeric ``server_time_offset_hours`` -> used as-is (explicit override,
      e.g. for historical backfills);
    * ``"auto"`` -> measured from a fresh live tick at startup, falling back
      to ``server_time_offset_hours_fallback`` (0.0 if unset) when the market
      is closed or the terminal is unreachable;
    * missing / unparseable -> 0.0 (legacy server-as-UTC behaviour).

    Returns ``(offset, info)`` where ``info`` explains how the offset was
    decided (``mode`` + ``reason``), so startup logs can show the WHY, not
    just the value.
    """
    raw = (market_data or {}).get("server_time_offset_hours", 0.0)
    if isinstance(raw, (int, float)):
        return float(raw), {"mode": "explicit", "reason": f"config={raw!r}"}
    if str(raw).strip().lower() == "auto":
        fallback = float((market_data or {}).get("server_time_offset_hours_fallback", 0.0))
        offset, info = detect_server_offset_hours_detailed(fallback=fallback)
        info["fallback"] = fallback
        return offset, info
    try:
        parsed = float(raw)
        return parsed, {"mode": "explicit", "reason": f"config={raw!r}"}
    except (TypeError, ValueError):
        logger.warning("invalid server_time_offset_hours %r; defaulting to 0.0", raw)
        return 0.0, {"mode": "invalid", "reason": f"unparseable config={raw!r}"}


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
