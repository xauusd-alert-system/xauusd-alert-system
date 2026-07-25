"""
Candle anatomy features: body ratio, upper/lower wick ratios, candle direction.
All computed row-wise from that row's own OHLC only - inherently causal, zero look-ahead risk.
"""
import numpy as np
import pandas as pd


def candle_anatomy(df: pd.DataFrame) -> pd.DataFrame:
    """
    body_ratio: |close-open| / (high-low)  -> how much of the candle range is "body"
    upper_wick_ratio: (high - max(open,close)) / (high-low)
    lower_wick_ratio: (min(open,close) - low) / (high-low)
    direction: +1 bullish, -1 bearish, 0 doji (open == close)
    All ratios are NaN-safe when high == low (zero-range candle, rare but possible on illiquid feeds).
    """
    out = df.copy()
    rng = (out["high"] - out["low"]).replace(0, np.nan)

    body = (out["close"] - out["open"]).abs()
    upper_wick = out["high"] - out[["open", "close"]].max(axis=1)
    lower_wick = out[["open", "close"]].min(axis=1) - out["low"]

    out["body_ratio"] = (body / rng).fillna(0.0)
    out["upper_wick_ratio"] = (upper_wick / rng).fillna(0.0)
    out["lower_wick_ratio"] = (lower_wick / rng).fillna(0.0)
    out["candle_direction"] = np.sign(out["close"] - out["open"]).astype(int)

    return out
