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

def _trend_votes(df: pd.DataFrame, n_agents: int = 32) -> np.ndarray:
    """Trend agents: each with a slightly different EMA pair / ADX threshold."""
    close = df["close"]
    # base EMAs already in df if build_all_indicators ran; fall back to local calc
    ema_fast = df["ema_20"] if "ema_20" in df.columns else _ema(close, 20)
    ema_slow = df["ema_50"] if "ema_50" in df.columns else _ema(close, 50)
    slope_fast = ema_fast.diff(5)
    slope_slow = ema_slow.diff(10)
    adx = df["adx"] if "adx" in df.columns else pd.Series(15.0, index=df.index)

    votes = np.zeros((len(df), n_agents), dtype=np.int8)
    rng = np.random.default_rng(42)
    for k in range(n_agents):
        # jitter thresholds so population is not clones
        adx_thr = 18 + rng.uniform(-4, 6)
        slope_eps = rng.uniform(0.0, 0.15)
        long_cond = (slope_fast > slope_eps) & (slope_slow > 0) & (adx > adx_thr) & (close > ema_fast)
        short_cond = (slope_fast < -slope_eps) & (slope_slow < 0) & (adx > adx_thr) & (close < ema_fast)
        v = np.zeros(len(df), dtype=np.int8)
        v[long_cond.values] = 1
        v[short_cond.values] = -1
        votes[:, k] = v
    return votes


def _countertrend_votes(df: pd.DataFrame, n_agents: int = 32) -> np.ndarray:
    """Counter-trend agents: Donchian extremes + RSI."""
    close = df["close"]
    high, low = df["high"], df["low"]
    # Donchian 20 already in df if built; fallback
    don_hi = df["donchian_high_20"] if "donchian_high_20" in df.columns else high.rolling(20).max()
    don_lo = df["donchian_low_20"] if "donchian_low_20" in df.columns else low.rolling(20).min()
    rsi = df["rsi"] if "rsi" in df.columns else _rsi(close, 14)

    votes = np.zeros((len(df), n_agents), dtype=np.int8)
    rng = np.random.default_rng(123)
    for k in range(n_agents):
        rsi_lo = 28 + rng.uniform(-5, 5)
        rsi_hi = 72 + rng.uniform(-5, 5)
        dist_hi = (close - don_hi).abs()
        dist_lo = (close - don_lo).abs()
        # near upper Donchian + overbought -> short; near lower + oversold -> long
        long_cond = (dist_lo < dist_hi) & (rsi < rsi_lo)
        short_cond = (dist_hi < dist_lo) & (rsi > rsi_hi)
        v = np.zeros(len(df), dtype=np.int8)
        v[long_cond.values] = 1
        v[short_cond.values] = -1
        votes[:, k] = v
    return votes


def _noise_votes(n_rows: int, n_agents: int = 16, seed: int = 999) -> np.ndarray:
    """Noise baseline: uniform random votes. Anchors entropy away from 0 when
    informed agents abstain (all 0)."""
    rng = np.random.default_rng(seed)
    # 60% flat, 20% long, 20% short — abstention-heavy
    return rng.choice([-1, 0, 1], size=(n_rows, n_agents), p=[0.2, 0.6, 0.2]).astype(np.int8)


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


def add_bifurcation_features(df: pd.DataFrame,
                             n_trend: int = 32,
                             n_counter: int = 32,
                             n_noise: int = 16) -> pd.DataFrame:
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
    out["agent_long_ratio"] = np.where(total_dir > 0, long_cnt / total_dir, 0.5)
    out["agent_short_ratio"] = np.where(total_dir > 0, short_cnt / total_dir, 0.5)

    # intensity amplifier: order-flow divergence + BB squeeze
    cvd_slope = out["cvd_slope_10"].abs() if "cvd_slope_10" in out.columns else pd.Series(0.0, index=out.index)
    # squeeze = 1 - percentile (0 = wide, 1 = tight)
    if "bb_width_percentile" in out.columns:
        squeeze = (100 - out["bb_width_percentile"].fillna(50)) / 100.0
    elif "bb_width_minmax_100" in out.columns:
        squeeze = 1.0 - out["bb_width_minmax_100"].fillna(0.5)
    else:
        squeeze = pd.Series(0.0, index=out.index)
    out["break_intensity"] = (out["break_score"] * (1.0 + cvd_slope.fillna(0.0).clip(0, 3) * 0.3 + squeeze.fillna(0.0) * 0.4)).clip(0, 1.5)

    return out
