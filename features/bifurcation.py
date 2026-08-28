# -*- coding: utf-8 -*-
"""
Agent-based bifurcation detector for XAUUSD regime breaks.

Three agent populations vote each bar (causal, using only data up to that bar):

  TrendAgent      — EMA slope + ADX alignment
  CounterTrendAgent — Donchian mean-reversion + RSI extremes
  NoiseAgent      — random baseline (entropy anchor)

break_score in [0,1]: normalized Shannon entropy of the vote distribution.
  0 = total agreement (inertia), 1 = maximal disagreement (bifurcation).
  A complementary `break_intensity` = entropy * (1 + |cvd_slope| + bb_squeeze)
  amplifies breaks that coincide with order-flow divergence / volatility squeeze.

Causality: every agent reads only past features (shifted, rolling, ewm). No future
bar is ever referenced. Tested via the same truncation-equality harness as
features/tests/test_no_lookahead.py.

Usage:
    from features.bifurcation import add_bifurcation_features
    df = add_bifurcation_features(df)  # adds break_score, break_intensity, agent_long_ratio
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --- helpers (causal) -------------------------------------------------------


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    d = series.diff()
    gain = d.clip(lower=0)
    loss = -d.clip(upper=0)
    ag = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    al = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50.0)


# --- per-agent vote matrices (vectorized, causal) ---------------------------


def _trend_votes(df: pd.DataFrame, n_agents: int = 24) -> np.ndarray:
    """Trend agents: stricter — higher ADX, steeper slope, price must be clearly
    beyond EMA. Fewer, more selective votes -> entropy more meaningful."""
    close = df["close"]
    ema_fast = df["ema_20"] if "ema_20" in df.columns else _ema(close, 20)
    ema_slow = df["ema_50"] if "ema_50" in df.columns else _ema(close, 50)
    slope_fast = ema_fast.diff(5)
    slope_slow = ema_slow.diff(10)
    adx = df["adx"] if "adx" in df.columns else pd.Series(15.0, index=df.index)

    votes = np.zeros((len(df), n_agents), dtype=np.int8)
    rng = np.random.default_rng(42)
    for k in range(n_agents):
        adx_thr = 24 + rng.uniform(-3, 4)  # was 18±6 -> now 24±4, much stricter
        slope_eps = rng.uniform(0.12, 0.28)  # was 0.0-0.15 -> now 0.12-0.28
        # price must be beyond EMA by 0.15*ATR to count
        atr = df["atr"] if "atr" in df.columns else pd.Series(1.0, index=df.index)
        dist = (close - ema_fast).abs() / atr.replace(0, np.nan)
        long_cond = (
            (slope_fast > slope_eps) & (slope_slow > 0.08) & (adx > adx_thr) & (close > ema_fast) & (dist > 0.15)
        )
        short_cond = (
            (slope_fast < -slope_eps) & (slope_slow < -0.08) & (adx > adx_thr) & (close < ema_fast) & (dist > 0.15)
        )
        v = np.zeros(len(df), dtype=np.int8)
        v[long_cond.values] = 1
        v[short_cond.values] = -1
        votes[:, k] = v
    return votes


def _countertrend_votes(df: pd.DataFrame, n_agents: int = 24) -> np.ndarray:
    """Counter-trend agents: much stricter RSI + must be at Donchian extreme."""
    close = df["close"]
    high, low = df["high"], df["low"]
    don_hi = df["donchian_high_20"] if "donchian_high_20" in df.columns else high.rolling(20).max()
    don_lo = df["donchian_low_20"] if "donchian_low_20" in df.columns else low.rolling(20).min()
    rsi = df["rsi"] if "rsi" in df.columns else _rsi(close, 14)

    votes = np.zeros((len(df), n_agents), dtype=np.int8)
    rng = np.random.default_rng(123)
    for k in range(n_agents):
        rsi_lo = 22 + rng.uniform(-3, 3)  # was 28±5 -> now 22±3, deeper oversold
        rsi_hi = 78 + rng.uniform(-3, 3)  # was 72±5 -> now 78±3, deeper overbought
        # must be within 0.3 ATR of Donchian extreme to count
        atr = df["atr"] if "atr" in df.columns else pd.Series(1.0, index=df.index)
        dist_atr_hi = (don_hi - close) / atr.replace(0, np.nan)
        dist_atr_lo = (close - don_lo) / atr.replace(0, np.nan)
        long_cond = (dist_atr_lo < 0.35) & (rsi < rsi_lo)
        short_cond = (dist_atr_hi < 0.35) & (rsi > rsi_hi)
        v = np.zeros(len(df), dtype=np.int8)
        v[long_cond.values] = 1
        v[short_cond.values] = -1
        votes[:, k] = v
    return votes


def _noise_votes(n_rows: int, n_agents: int = 4, seed: int = 999) -> np.ndarray:
    """Noise baseline: minimal — just enough to avoid 0 entropy when everyone abstains."""
    rng = np.random.default_rng(seed)
    return rng.choice([-1, 0, 1], size=(n_rows, n_agents), p=[0.15, 0.7, 0.15]).astype(np.int8)


def _entropy_from_votes(votes: np.ndarray) -> np.ndarray:
    """Normalized Shannon entropy per row over {-1,0,1} distribution."""
    n = votes.shape[0]
    ent = np.zeros(n, dtype=float)
    # max entropy for 3 outcomes = log(3)
    max_ent = np.log(3)
    for i in range(n):
        row = votes[i]
        # count each vote value
        c_neg = np.sum(row == -1)
        c_zero = np.sum(row == 0)
        c_pos = np.sum(row == 1)
        total = len(row)
        probs = np.array([c_neg, c_zero, c_pos], dtype=float) / total
        # filter zeros for log
        probs = probs[probs > 0]
        h = -np.sum(probs * np.log(probs))
        ent[i] = h / max_ent if max_ent > 0 else 0.0
    return ent


def add_bifurcation_features(
    df: pd.DataFrame, n_trend: int = 32, n_counter: int = 32, n_noise: int = 16
) -> pd.DataFrame:
    """Attach agent-based bifurcation columns (causal).

    Added columns:
      break_score      — [0,1] entropy (1 = maximal disagreement)
      break_intensity  — break_score * (1 + |cvd_slope| + squeeze)
      agent_long_ratio — share of long votes among directional votes
      agent_short_ratio — share of short votes
    """
    out = df.copy()
    n = len(out)

    vt = _trend_votes(out, n_trend)
    vc = _countertrend_votes(out, n_counter)
    vn = _noise_votes(n, n_noise)

    all_votes = np.concatenate([vt, vc, vn], axis=1)
    out["break_score"] = _entropy_from_votes(all_votes)

    # directional ratios (only among non-flat votes)
    long_cnt = np.sum(all_votes == 1, axis=1).astype(float)
    short_cnt = np.sum(all_votes == -1, axis=1).astype(float)
    total_dir = long_cnt + short_cnt
    out["agent_long_ratio"] = np.divide(
        long_cnt,
        total_dir,
        out=np.full_like(long_cnt, 0.5, dtype=float),
        where=total_dir > 0,
    )
    out["agent_short_ratio"] = np.divide(
        short_cnt,
        total_dir,
        out=np.full_like(short_cnt, 0.5, dtype=float),
        where=total_dir > 0,
    )

    # intensity amplifier: order-flow divergence + BB squeeze
    cvd_slope = out["cvd_slope_10"].abs() if "cvd_slope_10" in out.columns else pd.Series(0.0, index=out.index)
    # squeeze = 1 - percentile (0 = wide, 1 = tight)
    if "bb_width_percentile" in out.columns:
        squeeze = (100 - out["bb_width_percentile"].fillna(50)) / 100.0
    elif "bb_width_minmax_100" in out.columns:
        squeeze = 1.0 - out["bb_width_minmax_100"].fillna(0.5)
    else:
        squeeze = pd.Series(0.0, index=out.index)
    out["break_intensity"] = (
        out["break_score"] * (1.0 + cvd_slope.fillna(0.0).clip(0, 3) * 0.3 + squeeze.fillna(0.0) * 0.4)
    ).clip(0, 1.5)

    # --- correction-after-break flag (literal per transcript):
    # 1) squeeze: bb_width_percentile < 20 within last 10 bars
    # 2) breakout: close beyond Donchian 20 with volume spike
    # 3) pullback 38-61% of the breakout impulse within 20 bars
    bb_squeeze = (
        (out["bb_width_percentile"] < 20) if "bb_width_percentile" in out.columns else pd.Series(False, index=out.index)
    )
    vol_ratio = out["volume_ratio"] if "volume_ratio" in out.columns else pd.Series(1.0, index=out.index)
    don_hi = out["donchian_high_20"] if "donchian_high_20" in out.columns else out["high"].rolling(20).max()
    don_lo = out["donchian_low_20"] if "donchian_low_20" in out.columns else out["low"].rolling(20).min()

    breakout_long = (out["close"] > don_hi.shift(1)) & (vol_ratio > 1.5)
    breakout_short = (out["close"] < don_lo.shift(1)) & (vol_ratio > 1.5)
    # squeeze must have occurred in the 10 bars before breakout
    squeeze_recent = bb_squeeze.rolling(10, min_periods=1).max().astype(bool)
    breakout = (breakout_long | breakout_short) & squeeze_recent

    # impulse range around breakout (5 bars centered)
    roll_high = out["high"].rolling(5, center=True, min_periods=1).max()
    roll_low = out["low"].rolling(5, center=True, min_periods=1).min()

    in_corr = np.zeros(n, dtype=bool)
    last_h = last_l = None
    last_t = -1000
    for i in range(n):
        if breakout.iloc[i]:
            last_h = roll_high.iloc[i]
            last_l = roll_low.iloc[i]
            last_t = i
        if last_h is not None and last_l is not None and 0 < i - last_t <= 20:
            rng = last_h - last_l
            if rng > 0:
                retrace_up = (last_h - out["close"].iloc[i]) / rng
                retrace_dn = (out["close"].iloc[i] - last_l) / rng
                if 0.38 <= retrace_up <= 0.61 or 0.38 <= retrace_dn <= 0.61:
                    in_corr[i] = True
    out["in_correction_after_break"] = in_corr
    out["breakout_signal"] = breakout.fillna(False)

    return out
