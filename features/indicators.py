"""
Technical indicators: EMA, RSI, MACD, ATR, Bollinger Bands width.

CRITICAL NO-LOOK-AHEAD RULE:
Every function here computes indicator value at row i using ONLY rows [0..i].
This is naturally satisfied by pandas rolling/ewm operations because they are
causal by construction (a rolling window ending at i never touches i+1..n).
We still add explicit unit tests (see features/tests/test_no_lookahead.py) that
truncate the DataFrame at index i and assert the value is identical to the value
computed on the full DataFrame at row i - this proves no future leakage empirically,
not just by code inspection.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average. adjust=False = standard trading EMA recursion, causal."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing via ewm, causal)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)  # neutral RSI during warm-up, not NaN, to keep downstream code simple


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line, and histogram. All causal (ewm-based)."""
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame({"macd_line": macd_line, "macd_signal": signal_line, "macd_hist": hist})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range. True range uses previous close (df['close'].shift(1)),
    which is intentional and correct (TR is defined using prior close) - this is
    NOT look-ahead because shift(1) pulls from the PAST, not the future.
    """
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger_width(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands and normalized width (width / middle band, causal rolling stats)."""
    mid = series.rolling(window=period, min_periods=period).mean()
    std = series.rolling(window=period, min_periods=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    width = (upper - lower) / mid.replace(0, np.nan)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width})


def build_all_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Compute the full indicator set from config-driven parameters (no hardcoded periods here).
    df must contain columns: open, high, low, close, volume, timestamp_utc.
    Returns a new DataFrame with indicator columns appended, same row count and index as input.
    """
    out = df.copy()
    for period in cfg["features"]["ema_periods"]:
        out[f"ema_{period}"] = ema(out["close"], period)

    out["rsi"] = rsi(out["close"], cfg["features"]["rsi_period"])

    macd_cfg = cfg["features"]["macd"]
    macd_df = macd(out["close"], macd_cfg["fast"], macd_cfg["slow"], macd_cfg["signal"])
    out = pd.concat([out, macd_df], axis=1)

    out["atr"] = atr(out, cfg["features"]["atr_period"])

    bb_cfg = cfg["features"]["bollinger"]
    bb_df = bollinger_width(out["close"], bb_cfg["period"], bb_cfg["std_dev"])
    out = pd.concat([out, bb_df], axis=1)

    return out
