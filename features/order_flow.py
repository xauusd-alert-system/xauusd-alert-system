"""
Order Flow, Cumulative Volume Delta (CVD), and Microstructure features.
Strictly causal: all rolling windows and aggregations read only historical bars.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def cumulative_volume_delta(df: pd.DataFrame, window: int = 100) -> pd.Series:
    """
    Estimates Volume Delta from candle anatomy.

    N1 (audit 2026-08-10): the raw `delta_vol.cumsum()` accumulates from the START
    of whatever window is passed in, so the CVD level depended on the frame length
    — the same bar got a different feature value in the backtest fold frame vs the
    live pipeline window (train/serve skew). We now anchor CVD to a fixed trailing
    `window` of bars: cvd[i] = sum(delta_vol) over [i-window+1, i]. For bars past
    the anchoring window this value is invariant to the total frame length, so the
    distribution the model sees at inference matches training. Rows within the
    first `window` bars are a partial-sum warm-up (handled as neutral by the
    inference path).
    """
    hl_range = (df["high"] - df["low"]).replace(0, np.nan)
    # Location of close inside the candle range (-1.0 to +1.0)
    close_loc = ((df["close"] - df["low"]) / hl_range) * 2.0 - 1.0
    close_loc = close_loc.fillna(0.0).clip(-1.0, 1.0)

    # Delta volume estimate: signed portion of volume
    vol = df["volume"].fillna(0.0)
    delta_vol = close_loc * vol
    cum = delta_vol.cumsum()
    if window is not None and window > 0 and len(df) > 0:
        # cvd[i] = cum[i] - cum[i - window]; for i >= window this is the fixed
        # trailing-window cumulative delta (frame-length invariant).
        anchored = cum - cum.shift(window)
        # Warm-up partial sums keep cum from frame start (their values are not
        # used by the trading path once NaN features are neutralized upstream).
        anchored.iloc[:window] = cum.iloc[:window]
        return anchored.fillna(0.0)
    return cum


def order_flow_imbalance(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculates rolling buy/sell pressure imbalance over `period` bars.
    Returns value in range [-1.0, +1.0].
    """
    hl_range = (df["high"] - df["low"]).replace(0, np.nan)
    buy_pressure = ((df["close"] - df["low"]) / hl_range).fillna(0.5) * df["volume"].fillna(0.0)
    sell_pressure = ((df["high"] - df["close"]) / hl_range).fillna(0.5) * df["volume"].fillna(0.0)

    rolling_buy = buy_pressure.rolling(window=period, min_periods=1).sum()
    rolling_sell = sell_pressure.rolling(window=period, min_periods=1).sum()
    total_flow = (rolling_buy + rolling_sell).replace(0, np.nan)

    imbalance = (rolling_buy - rolling_sell) / total_flow
    return imbalance.fillna(0.0).clip(-1.0, 1.0)


def volume_weighted_average_price(
    df: pd.DataFrame, period: int = 72
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculates rolling causal VWAP and +/- 2 standard deviation bands.
    Typical price = (High + Low + Close) / 3.
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].fillna(0.0)

    vol_price = typical_price * vol
    rolling_vol_price = vol_price.rolling(window=period, min_periods=1).sum()
    rolling_vol = vol.rolling(window=period, min_periods=1).sum().replace(0, np.nan)

    vwap = (rolling_vol_price / rolling_vol).fillna(typical_price)

    # Rolling price variance relative to VWAP
    diff_sq = (typical_price - vwap) ** 2
    weighted_diff_sq = diff_sq * vol
    rolling_var = (
        weighted_diff_sq.rolling(window=period, min_periods=1).sum() / rolling_vol
    ).fillna(0.0)
    vwap_std = np.sqrt(np.maximum(rolling_var, 0.0))

    vwap_upper = vwap + 2.0 * vwap_std
    vwap_lower = vwap - 2.0 * vwap_std

    return vwap, vwap_upper, vwap_lower


def add_order_flow_features(df: pd.DataFrame, cvd_window: int = 100) -> pd.DataFrame:
    """
    Computes and attaches all order flow and microstructure features causally.

    cvd_window: fixed trailing window (bars) the CVD level is anchored to so it is
    invariant to frame length (N1). Pass the same value at train and inference.
    """
    out = df.copy()
    out["cvd"] = cumulative_volume_delta(out, window=cvd_window)
    out["cvd_slope_10"] = out["cvd"].diff(10).fillna(0.0)
    out["order_flow_imbalance_14"] = order_flow_imbalance(out, period=14)
    out["order_flow_imbalance_50"] = order_flow_imbalance(out, period=50)

    vwap, vwap_upper, vwap_lower = volume_weighted_average_price(out, period=72)
    out["vwap"] = vwap
    out["vwap_upper"] = vwap_upper
    out["vwap_lower"] = vwap_lower
    
    # Distance from close to VWAP normalized by ATR (or price if ATR missing)
    denom = out["atr"] if "atr" in out.columns else out["close"] * 0.001
    denom = denom.replace(0, np.nan)
    out["dist_vwap_atr"] = ((out["close"] - out["vwap"]) / denom).fillna(0.0)

    return out
