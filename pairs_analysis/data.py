# -*- coding: utf-8 -*-
"""Data layer for the pairs-analysis module (ТЗ §3).

Sources:
  - MT5 broker sqlite (data/market_data_mt5.sqlite: ohlcv_m5/m15/h1) — the
    forex/metals instruments the main software trades (XAUUSD, XAGUSD,
    EURUSD, GBPUSD, BTCUSD). Any requested timeframe is resampled up from
    the finest available table.
  - CSV import for offline analysis/tests (ТЗ §3): flexible column names.
  - Public Binance REST (klines) for crypto pairs not in MT5 (BTC/ETH/SOL),
    cached in data/pairs_cache.sqlite so offline re-analysis works.

All series are returned indexed by UTC timestamps. Alignment (ТЗ §3) drops
bars missing in either instrument (inner join).
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "market_data_mt5.sqlite")
DEFAULT_CACHE = os.path.join(ROOT, "data", "pairs_cache.sqlite")

# ---------- In-memory cache (survives for the process lifetime) ----------
# Keyed by (symbol, timeframe, db_path, db_file_mtime) → pd.DataFrame
_mt5_mem_cache: dict[tuple, pd.DataFrame] = {}

# MT5 table per timeframe; H4/D1 are always built by resampling.
MT5_TABLES = {"M5": "ohlcv_m5", "M15": "ohlcv_m15", "H1": "ohlcv_h1"}
TF_ORDER = ["M5", "M15", "H1", "H4", "D1"]
RESAMPLE_RULES = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}
BINANCE_INTERVALS = {"M5": "5m", "M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}
BINANCE_URL = "https://api.binance.com/api/v3/klines"

# CSV column aliases (matched case-insensitively on the header).
CSV_ALIASES = {
    "timestamp": ["timestamp", "time", "datetime", "date", "open_time", "ts"],
    "open": ["open", "o"],
    "high": ["high", "h"],
    "low": ["low", "l"],
    "close": ["close", "c"],
    "volume": ["volume", "vol", "v"],
}


def _norm_tf(timeframe: str) -> str:
    tf = str(timeframe).strip().upper()
    if tf not in TF_ORDER:
        raise ValueError(f"неизвестный таймфрейм {timeframe!r}; допустимые: {TF_ORDER}")
    return tf


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate an OHLCV frame (UTC index) to a coarser timeframe."""
    tf = _norm_tf(timeframe)
    rule = RESAMPLE_RULES[tf]
    agg = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return agg.dropna(subset=["open", "high", "low", "close"])


def _mt5_disk_cache_key(symbol: str, tf: str, db_path: str) -> str:
    """Deterministic key for the on-disk MT5 resampled cache."""
    return f"{symbol}|{tf}|{os.path.getmtime(db_path):.0f}"


def _mt5_disk_cache_get(key: str, cache_path: str = DEFAULT_CACHE) -> pd.DataFrame | None:
    """Try to load a cached resampled MT5 frame from disk."""
    if not os.path.exists(cache_path):
        return None
    try:
        con = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT open_time, open, high, low, close, volume FROM mt5_cache WHERE cache_key=? ORDER BY open_time",
                (key,),
            ).fetchall()
            if not row:
                return None
            df = pd.DataFrame(row, columns=["time", "open", "high", "low", "close", "volume"])
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.set_index("time").sort_index()
            return df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
        finally:
            con.close()
    except Exception:
        return None


def _mt5_disk_cache_put(df: pd.DataFrame, key: str, cache_path: str = DEFAULT_CACHE) -> None:
    """Persist a resampled MT5 frame to disk cache."""
    _init_cache(cache_path)
    con = sqlite3.connect(cache_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS mt5_cache ("
            " cache_key TEXT, open_time INTEGER, "
            " open REAL, high REAL, low REAL, close REAL, volume REAL, "
            " PRIMARY KEY (cache_key, open_time))"
        )
        con.execute("DELETE FROM mt5_cache WHERE cache_key=?", (key,))
        con.executemany(
            "INSERT INTO mt5_cache (cache_key, open_time, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
            [
                (key, int(ts.timestamp()), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
                for ts, r in df.iterrows()
            ],
        )
        con.commit()
    finally:
        con.close()


def load_mt5(
    symbol: str, timeframe: str = "D1", db_path: str = DEFAULT_DB, _cache_path: str = DEFAULT_CACHE
) -> pd.DataFrame:
    """OHLCV for one MT5 symbol on the requested timeframe (resampled up from
    the finest available table). Two-level cache:
      1. In-memory dict (same process, same db_mtime → instant)
      2. On-disk sqlite in pairs_cache.sqlite (cross-process, same db_mtime)"""
    tf = _norm_tf(timeframe)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"MT5-база не найдена: {db_path}")

    # --- cache key includes db file mtime so stale caches are ignored ---
    try:
        mtime = os.path.getmtime(db_path)
    except OSError:
        mtime = 0
    mem_key = (symbol, tf, db_path, mtime)
    if mem_key in _mt5_mem_cache:
        return _mt5_mem_cache[mem_key].copy()

    disk_key = _mt5_disk_cache_key(symbol, tf, db_path)
    cached = _mt5_disk_cache_get(disk_key, _cache_path)
    if cached is not None and len(cached) > 0:
        _mt5_mem_cache[mem_key] = cached
        return cached.copy()

    # --- cache miss: read from source + resample ---
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        available = []
        for cand in ("H1", "M15", "M5"):
            table = MT5_TABLES[cand]
            n = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE symbol=?", (symbol,)).fetchone()[0]
            if n:
                available.append(cand)
        if not available:
            raise FileNotFoundError(f"символ {symbol} отсутствует в MT5-базе")
        candidates = [c for c in available if TF_ORDER.index(c) <= TF_ORDER.index(tf)]
        source_tf = candidates[0] if candidates else available[0]
        table = MT5_TABLES[source_tf]
        rows = cur.execute(
            f"SELECT timestamp_utc, open, high, low, close, volume FROM {table} WHERE symbol=? ORDER BY timestamp_utc",
            (symbol,),
        ).fetchall()
    finally:
        con.close()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time").sort_index()
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    result = resample(df, tf) if source_tf != tf else df

    # --- populate both caches ---
    _mt5_mem_cache[mem_key] = result
    _mt5_disk_cache_put(result, disk_key, _cache_path)
    return result.copy()


def load_csv(path: str, timeframe: str | None = None) -> pd.DataFrame:
    """CSV import (ТЗ §3): flexible column names; returns UTC-indexed OHLCV."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV не найден: {path}")
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    lower = {c.lower(): c for c in df.columns}
    rename = {}
    for std, aliases in CSV_ALIASES.items():
        for a in aliases:
            if a in lower:
                rename[lower[a]] = std
                break
    df = df.rename(columns=rename)
    missing = [c for c in ("timestamp", "open", "high", "low", "close") if c not in df]
    if missing:
        raise ValueError(f"CSV {path}: нет колонок {missing} (распознаны: {list(df.columns)})")
    if "volume" not in df:
        df["volume"] = 0.0
    ts = df["timestamp"]
    if pd.api.types.is_numeric_dtype(ts):
        # epoch seconds if small, millis if large
        unit = "s" if ts.max() < 10**11 else "ms"
        t = pd.to_datetime(ts, unit=unit, utc=True)
    else:
        t = pd.to_datetime(ts, utc=True)
    # NOTE: to_numeric keeps the CSV RangeIndex — pass .to_numpy() so the new
    # DatetimeIndex is positional, not aligned (index mismatch would NaN all rows).
    out = pd.DataFrame(
        {
            "open": pd.to_numeric(df["open"], errors="coerce").to_numpy(),
            "high": pd.to_numeric(df["high"], errors="coerce").to_numpy(),
            "low": pd.to_numeric(df["low"], errors="coerce").to_numpy(),
            "close": pd.to_numeric(df["close"], errors="coerce").to_numpy(),
            "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0.0).to_numpy(),
        },
        index=t,
    )
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=["open", "high", "low", "close"])
    if timeframe:
        out = resample(out, timeframe)
    return out


def align(df1: pd.DataFrame, df2: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Inner join on the timestamp index: drops bars missing in either leg
    (ТЗ §3 — e.g. crypto 24/7 vs forex weekends)."""
    idx = df1.index.intersection(df2.index)
    return df1.loc[idx], df2.loc[idx]


# ---------------------------------------------------------------------------
# Binance public REST + sqlite cache (BTC/ETH/SOL pairs, no API keys needed)
# ---------------------------------------------------------------------------


def _init_cache(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS binance_klines ("
            " symbol TEXT, interval TEXT, open_time INTEGER, "
            " open REAL, high REAL, low REAL, close REAL, volume REAL, "
            " PRIMARY KEY (symbol, interval, open_time))"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS mt5_cache ("
            " cache_key TEXT, open_time INTEGER, "
            " open REAL, high REAL, low REAL, close REAL, volume REAL, "
            " PRIMARY KEY (cache_key, open_time))"
        )
        con.commit()
    finally:
        con.close()


def _fetch_binance_pages(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    """Page through the public klines endpoint (max 1000 bars/request)."""
    out: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        r = requests.get(
            BINANCE_URL,
            params={"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        out.extend(data)
        last = data[-1][0]
        if last <= cursor:  # no progress guard
            break
        cursor = last + 1
    return out


def _klines_to_df(rows: list[list], meta: bool = True) -> pd.DataFrame:
    """API rows carry the full 12-field kline; cached rows only the 6 we store."""
    if meta:
        cols = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_base",
            "taker_quote",
            "ignore",
        ]
    else:
        cols = ["open_time", "open", "high", "low", "close", "volume"]
    df = pd.DataFrame(rows, columns=cols)
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def fetch_binance(
    symbol: str,
    timeframe: str = "D1",
    start: dt.date | dt.datetime | None = None,
    end: dt.date | dt.datetime | None = None,
    cache_path: str = DEFAULT_CACHE,
    lookback_days: int = 800,
) -> pd.DataFrame:
    """OHLCV for a Binance symbol, served from the sqlite cache when the
    requested range is fully covered, otherwise fetched from the public REST
    and upserted. Raises RuntimeError when the network fails and the cache
    does not cover the range."""
    tf = _norm_tf(timeframe)
    interval = BINANCE_INTERVALS[tf]
    if end is None:
        end = dt.datetime.now(dt.UTC)
    if start is None:
        start = end - dt.timedelta(days=lookback_days)

    def _ms(t):
        ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return int(ts.timestamp() * 1000)

    start_ms = _ms(start)
    end_ms = _ms(end)

    _init_cache(cache_path)
    con = sqlite3.connect(cache_path)
    try:
        rows = con.execute(
            "SELECT open_time, open, high, low, close, volume FROM binance_klines "
            "WHERE symbol=? AND interval=? AND open_time>=? AND open_time<=? "
            "ORDER BY open_time",
            (symbol, interval, start_ms, end_ms),
        ).fetchall()
        # Allow tolerance of 2 intervals: last bar may be up to 2 periods old
        # (e.g. H1 data cached 40 min ago is still "covered" for D1 analysis)
        interval_ms = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(
            interval, 3_600_000
        )
        covered = len(rows) >= 2 and rows[0][0] <= start_ms + interval_ms and rows[-1][0] >= end_ms - 2 * interval_ms
        if covered:
            return _klines_to_df(rows, meta=False)
    finally:
        con.close()

    try:
        data = _fetch_binance_pages(symbol, interval, start_ms, end_ms)
    except (requests.RequestException, ValueError) as exc:
        if rows:  # stale cache: better than nothing
            return _klines_to_df(rows, meta=False)
        raise RuntimeError(f"Binance недоступен ({symbol} {interval}): {exc}") from exc

    df = _klines_to_df(data)
    con = sqlite3.connect(cache_path)
    try:
        con.executemany(
            "INSERT OR REPLACE INTO binance_klines "
            "(symbol, interval, open_time, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    symbol,
                    interval,
                    int(ts.timestamp() * 1000),
                    float(r.open),
                    float(r.high),
                    float(r.low),
                    float(r.close),
                    float(r.volume),
                )
                for ts, r in df.iterrows()
            ],
        )
        con.commit()
    finally:
        con.close()
    return df
