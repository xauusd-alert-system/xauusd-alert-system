"""
Rule-based market regime classifier.

Regimes: trend-up, trend-down, range, compression, reversal-watch, no-trade.

Design decision: this is a pure function of the LATEST row's feature values
(computed causally upstream in features/). No regime logic here looks at
future rows - it only reads columns already present on row i.

An ML-ready interface is defined separately in regime/ml_interface.py so this
rule-based classifier can be swapped for a trained classifier later without
changing the contract that regime/classifier.py -> RegimeLabel.

All thresholds come from config.yaml under `regime:` - nothing hardcoded here.
"""
from enum import Enum
import numpy as np
import pandas as pd


class RegimeLabel(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    COMPRESSION = "compression"
    REVERSAL_WATCH = "reversal_watch"
    NO_TRADE = "no_trade"


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Standard ADX (Average Directional Index) - causal by construction (rolling/ewm only).
    Included here rather than features/indicators.py because ADX is used exclusively
    for regime detection, not as a general-purpose feature for the ML model.
    """
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_smooth = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_smooth.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_smooth.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


def add_regime_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Adds adx, plus_di, minus_di, bb_width_percentile, atr_ratio columns needed for classification."""
    out = df.copy()
    adx_period = cfg["features"]["atr_period"]  # reuse ATR period config for ADX smoothing consistency
    adx, plus_di, minus_di = _compute_adx(out, adx_period)
    out["adx"] = adx
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di

    # bb_width must already exist (from features/indicators.py::build_all_indicators)
    if "bb_width" in out.columns:
        rolling = out["bb_width"].rolling(window=50, min_periods=20)
        out["bb_width_percentile"] = rolling.apply(
            lambda x: float(pd.Series(x).rank(pct=True).iloc[-1] * 100), raw=False
        )
    else:
        out["bb_width_percentile"] = np.nan

    if "atr" in out.columns:
        atr_rolling_mean = out["atr"].rolling(window=50, min_periods=10).mean()
        out["atr_ratio"] = out["atr"] / atr_rolling_mean.replace(0, np.nan)
    else:
        out["atr_ratio"] = np.nan

    return out


def classify_regime_row(row: pd.Series, cfg: dict) -> RegimeLabel:
    """
    Classify a single row (Series with all indicator + regime columns) into a RegimeLabel.
    Order of checks matters: no_trade and reversal_watch gates are checked FIRST
    because they represent risk-off conditions that should override a trend/range call.
    """
    regime_cfg = cfg["regime"]

    price = row.get("close", np.nan)
    atr_val = row.get("atr", np.nan)

    # Gate 1: insufficient data / warm-up period -> no_trade
    if pd.isna(row.get("adx")) or pd.isna(atr_val) or pd.isna(price):
        return RegimeLabel.NO_TRADE

    # Gate 2: volatility floor - if ATR/price ratio too low, spreads/slippage would dominate any edge
    if price > 0 and (atr_val / price) < regime_cfg["no_trade_volatility_floor"]:
        return RegimeLabel.NO_TRADE

    # Gate 3: volatility spike -> reversal-watch (market is unstable, wait for confirmation)
    atr_ratio = row.get("atr_ratio", np.nan)
    if not pd.isna(atr_ratio) and atr_ratio > regime_cfg["atr_spike_multiplier"]:
        return RegimeLabel.REVERSAL_WATCH

    # Gate 4: compression - Bollinger width in bottom percentile -> squeeze, breakout pending
    bb_pctile = row.get("bb_width_percentile", np.nan)
    if not pd.isna(bb_pctile) and bb_pctile < regime_cfg["bb_width_compression_pctile"]:
        return RegimeLabel.COMPRESSION

    # Gate 5: trend detection via ADX + DI direction
    adx_val = row.get("adx", 0.0)
    plus_di = row.get("plus_di", 0.0)
    minus_di = row.get("minus_di", 0.0)

    if adx_val >= regime_cfg["adx_trend_threshold"]:
        if plus_di > minus_di:
            return RegimeLabel.TREND_UP
        elif minus_di > plus_di:
            return RegimeLabel.TREND_DOWN

    # Default: no strong trend, no compression, no volatility spike -> range-bound
    return RegimeLabel.RANGE


def classify_regime_series(df: pd.DataFrame, cfg: dict) -> pd.Series:
    min_candles = cfg["regime"]["min_candles_for_regime"]
    raw_labels = [classify_regime_row(row, cfg) for _, row in df.iterrows()]
    labels = pd.Series(raw_labels, index=df.index, dtype=object)
    labels.iloc[:min_candles] = RegimeLabel.NO_TRADE
    return labels


def regime_onehot_df(df: pd.DataFrame, regimes=None) -> pd.DataFrame:
    """Expand a causal `regime` column into <regime_<label>> one-hot columns (Phase 3).

    Reads ONLY row i's own regime value (a RegimeLabel enum or its .value string,
    computed causally upstream) - never any future row, so it is drop-in safe for
    both training and realtime inference. Columns are fixed to the full RegimeLabel
    set so a regime absent from a dataset still gets an explicit 0 column, keeping
    training and inference feature spaces identical.

    Returns a DataFrame with one int column per regime: 1 where the row's regime
    matches, 0 otherwise. A missing/NaN regime produces all-0 columns (safe no-op
    encoding; callers should normally not feed such rows to the model anyway).
    """
    regimes = regimes or [r for r in RegimeLabel]
    names = [f"regime_{r.value}" for r in regimes]

    raw = df["regime"] if "regime" in df.columns else pd.Series(pd.NA, index=df.index)
    encoded = {}
    for r in regimes:
        # Accept both the enum member and its .value string; NaN/unmatched -> 0.
        encoded[f"regime_{r.value}"] = (
            raw.map(lambda v: 1 if (isinstance(v, RegimeLabel) and v is r)
                    or (not isinstance(v, RegimeLabel) and str(v if v is not None else "") == r.value)
                    else 0).astype(int)
            if len(raw)
            else pd.Series(0, index=df.index, dtype=int)
        )
    return pd.DataFrame(encoded, index=df.index, columns=names)

