import time
import numpy as np
import requests
import pandas as pd

from config.loader import get_env
from data.session_tagger import tag_dataframe


TIMEFRAME_TO_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}

TWELVE_DATA_MAX_OUTPUTSIZE = 5000
_MIN_SECONDS_BETWEEN_REQUESTS = 60 / 8  # 8 requests/minute free-tier limit


def fetch_mock_candles(timeframe: str, n_candles: int, sessions_config: dict,
                        end_ts: int = None, seed: int = 42) -> pd.DataFrame:
    """
    Generate a deterministic, reproducible synthetic OHLCV series for offline testing.
    Uses a random walk around a plausible XAUUSD price level (~2000-2600 range as of 2026).
    This is ONLY for testing the pipeline shape - never used for real signal generation.
    """
    if timeframe not in TIMEFRAME_TO_SECONDS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    rng = np.random.default_rng(seed)
    step_seconds = TIMEFRAME_TO_SECONDS[timeframe]
    if end_ts is None:
        end_ts = int(time.time())
    end_ts = (end_ts // step_seconds) * step_seconds

    timestamps = [end_ts - i * step_seconds for i in range(n_candles)][::-1]

    price = 2400.0
    rows = []
    for ts in timestamps:
        drift = rng.normal(0, 1.5)
        open_p = price
        close_p = open_p + drift
        high_p = max(open_p, close_p) + abs(rng.normal(0, 1.0))
        low_p = min(open_p, close_p) - abs(rng.normal(0, 1.0))
        vol = abs(rng.normal(100, 20))
        rows.append([ts, open_p, high_p, low_p, close_p, vol])
        price = close_p

    df = pd.DataFrame(rows, columns=["timestamp_utc", "open", "high", "low", "close", "volume"])
    df = tag_dataframe(df, sessions_config)
    return df


def _request_with_backoff(url: str, params: dict, max_retries: int = 5) -> dict:
    """Perform a GET request with exponential backoff on 429s and API-level rate limit errors."""
    delay = 2
    for attempt in range(max_retries):
        response = requests.get(url, params=params, timeout=60)
        if response.status_code == 429:
            time.sleep(delay)
            delay *= 2
            continue
        data = response.json()
        if isinstance(data, dict) and data.get("code") == 429:
            time.sleep(delay)
            delay *= 2
            continue
        response.raise_for_status()
        return data
    raise RuntimeError(f"Exceeded max retries ({max_retries}) due to rate limiting.")


def fetch_live_candles(timeframe: str, n_candles: int, sessions_config: dict,
                        api_key_env: str = "TWELVE_DATA_API_KEY",
                        base_url: str = "https://api.twelvedata.com/time_series") -> pd.DataFrame:
    """
    Fetch real OHLCV candles from a market data vendor.
    Requires API key set via environment variable (never hardcoded).
    """
    api_key = get_env(api_key_env, required=True)

    interval_map = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h"}
    if timeframe not in interval_map:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    if n_candles > TWELVE_DATA_MAX_OUTPUTSIZE:
        raise ValueError(f"n_candles exceeds Twelve Data max outputsize of {TWELVE_DATA_MAX_OUTPUTSIZE}")

    params = {
        "symbol": "XAU/USD",
        "interval": interval_map[timeframe],
        "outputsize": n_candles,
        "apikey": api_key,
        "timezone": "UTC",
        "format": "JSON",
    }

    resp = requests.get(base_url, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    if "values" not in payload:
        raise RuntimeError(f"Unexpected API response, no 'values' key: {payload}")

    rows = []
    for item in payload["values"]:
        ts = int(pd.Timestamp(item["datetime"], tz="UTC").timestamp())
        rows.append([
            ts,
            float(item["open"]),
            float(item["high"]),
            float(item["low"]),
            float(item["close"]),
            float(item.get("volume", 0.0) or 0.0),
        ])

    df = pd.DataFrame(rows, columns=["timestamp_utc", "open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp_utc").reset_index(drop=True)
    df = tag_dataframe(df, sessions_config)
    return df


def backfill_historical(timeframe: str, start_date: str, end_date: str, sessions_config: dict,
                         api_key_env: str = "TWELVE_DATA_API_KEY",
                         base_url: str = "https://api.twelvedata.com/time_series") -> pd.DataFrame:
    """Pull historical candles in 500-row chunks (free-tier safe)."""
    import time as _time
    api_key = get_env(api_key_env, required=True)
    interval_map = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h"}
    if timeframe not in interval_map:
        raise ValueError("Unsupported timeframe: " + timeframe)
    CHUNK_SIZE = 500
    all_rows = []
    current_end = end_date
    while True:
        params = {
            "symbol": "XAU/USD",
            "interval": interval_map[timeframe],
            "outputsize": CHUNK_SIZE,
            "end_date": current_end,
            "apikey": api_key,
            "timezone": "UTC",
            "format": "JSON",
        }
        data = _request_with_backoff(base_url, params)
        values = data.get("values", [])
        if not values:
            break
        values = [v for v in values if v["datetime"][:10] >= start_date]
        if not values:
            break
        all_rows.extend(values)
        oldest_ts = values[-1]["datetime"]
        if oldest_ts[:10] <= start_date:
            break
        current_end = oldest_ts
        _time.sleep(8)
    print()
    if not all_rows:
        raise RuntimeError("No data returned for " + timeframe + " between " + start_date + " and " + end_date)
    df = pd.DataFrame(all_rows)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates(subset="datetime").sort_values("datetime").reset_index(drop=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0) if "volume" in df.columns else 0.0
    df["timestamp_utc"] = df["datetime"].astype("int64") // 10**9
    df = df.drop(columns=["datetime"])
    df = df.drop_duplicates(subset="timestamp_utc").sort_values("timestamp_utc").reset_index(drop=True)
    df = tag_dataframe(df, sessions_config)
    return df


def fetch_candles(timeframe: str, n_candles: int, sessions_config: dict,
                   mode: str = "mock", **kwargs) -> pd.DataFrame:
    """
    Unified entry point used by downstream code.
    mode="mock" for offline/dev/test, mode="live" for real API pull.
    """
    if mode == "mock":
        return fetch_mock_candles(timeframe, n_candles, sessions_config, **kwargs)
    elif mode == "live":
        return fetch_live_candles(timeframe, n_candles, sessions_config, **kwargs)
    else:
        raise ValueError(f"Unknown ingestion mode: {mode}")

