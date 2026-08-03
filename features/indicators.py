"""
Technical indicators + Advanced Microstructure & Price Action features.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame({"macd_line": macd_line, "macd_signal": signal_line, "macd_hist": hist})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger_width(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    mid = series.rolling(window=period, min_periods=period).mean()
    std = series.rolling(window=period, min_periods=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    width = (upper - lower) / mid.replace(0, np.nan)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower, "bb_width": width})


def rolling_quantile(series: pd.Series, period: int, q: float) -> pd.Series:
    """Rolling quantile, causal only (uses past values)."""
    return series.rolling(window=period, min_periods=period).quantile(q)


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume: cumulative sum of signed volume."""
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def money_flow_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Money Flow Index."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mf = tp * df["volume"]
    pos = pd.Series(np.where(tp > tp.shift(1), mf, 0.0), index=df.index)
    neg = pd.Series(np.where(tp < tp.shift(1), mf, 0.0), index=df.index)
    pos_sum = pos.rolling(window=period, min_periods=period).sum()
    neg_sum = neg.rolling(window=period, min_periods=period).sum()
    mfi_val = 100 - 100 / (1 + pos_sum / neg_sum.replace(0, np.nan))
    return mfi_val.fillna(50.0)


def build_all_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = df.copy()

    # 1. Базовые индикаторы
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

    # 2. Динамика и объем
    out["return_1"] = np.log(out["close"] / out["close"].shift(1)).fillna(0.0)
    out["return_4"] = np.log(out["close"] / out["close"].shift(4)).fillna(0.0)

    vol_sma = out["volume"].rolling(window=20, min_periods=1).mean()
    out["volume_ratio"] = (out["volume"] / vol_sma.replace(0, np.nan)).fillna(1.0)
    out["atr_pct"] = (out["atr"] / out["close"]).fillna(0.0)

    atr_safe = out["atr"].replace(0, np.nan)

    # 3. Институциональные фичи (как раньше)
    log_hl = np.log(out["high"] / out["low"])
    log_co = np.log(out["close"] / out["open"])
    out["garman_klass_vol"] = np.sqrt(0.5 * (log_hl ** 2) - (2 * np.log(2) - 1) * (log_co ** 2)).fillna(0.0)

    out["dist_ema50_atr"] = ((out["close"] - out["ema_50"]) / atr_safe).fillna(0.0)
    out["dist_ema200_atr"] = ((out["close"] - out["ema_200"]) / atr_safe).fillna(0.0)

    out["macd_accel"] = out["macd_hist"].diff().fillna(0.0)

    ts = pd.to_datetime(out["timestamp_utc"], unit="s", utc=True)
    hours = ts.dt.hour + ts.dt.minute / 60.0
    out["sin_hour"] = np.sin(2 * np.pi * hours / 24.0)
    out["cos_hour"] = np.cos(2 * np.pi * hours / 24.0)

    day_group = out["timestamp_utc"] // 86400
    pdh = out["high"].groupby(day_group).transform("max").shift(288).ffill()
    pdl = out["low"].groupby(day_group).transform("min").shift(288).ffill()

    out["dist_pdh_atr"] = ((out["close"] - pdh) / atr_safe).fillna(0.0)
    out["dist_pdl_atr"] = ((out["close"] - pdl) / atr_safe).fillna(0.0)

    asia_mask = out["session"].str.contains("asia", na=False)
    asia_high = out["high"].where(asia_mask).groupby(day_group).transform("max").ffill()
    asia_low = out["low"].where(asia_mask).groupby(day_group).transform("min").ffill()

    out["dist_asia_high_atr"] = ((out["close"] - asia_high) / atr_safe).fillna(0.0)
    out["dist_asia_low_atr"] = ((out["close"] - asia_low) / atr_safe).fillna(0.0)

    # ===== НОВЫЕ ФИЧИ =====

    # 4. Объёмные / микроструктурные
    out["obv"] = obv(out).fillna(0)
    out["mfi"] = money_flow_index(out, 14).fillna(50.0)
    out["rsi_slope"] = out["rsi"].diff(5).fillna(0.0)
    out["volume_zscore"] = (
        (out["volume"] - out["volume"].rolling(20).mean()) / out["volume"].rolling(20).std()
    ).fillna(0.0)

    # 5. Donchian-каналы (20 баров)
    out["donchian_high_20"] = out["high"].rolling(20).max()
    out["donchian_low_20"] = out["low"].rolling(20).min()
    out["dist_donchian_high_atr"] = ((out["close"] - out["donchian_high_20"]) / atr_safe).fillna(0.0)
    out["dist_donchian_low_atr"] = ((out["close"] - out["donchian_low_20"]) / atr_safe).fillna(0.0)

    # 6. Процентили волатильности (100 баров)
    bb_width_min = out["bb_width"].rolling(100).min()
    bb_width_max = out["bb_width"].rolling(100).max()
    out["bb_width_percentile"] = ((out["bb_width"] - bb_width_min) / (bb_width_max - bb_width_min)).fillna(0.0).clip(0, 1)

    atr_min = atr_safe.rolling(100).min()
    atr_max = atr_safe.rolling(100).max()
    out["atr_percentile"] = ((atr_safe - atr_min) / (atr_max - atr_min)).fillna(0.0).clip(0, 1)

    return out