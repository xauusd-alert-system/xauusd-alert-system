# -*- coding: utf-8 -*-
"""Core pair metrics (ТЗ §4.1): hedge ratio β (Kalman / OLS), log-spread,
z-score, ADF p-value, OU half-life, spread volatility, price ratio.

All estimators are POINT-IN-TIME: the spread at bar t uses β_t estimated
from data up to t (Kalman filter state), and rolling windows use only past
bars — no look-ahead (ТЗ §7.2).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.tsa.stattools import adfuller


# ---------------------------------------------------------------------------
# Hedge ratio β
# ---------------------------------------------------------------------------

def ols_beta(x: pd.Series, y: pd.Series, window: int = 90) -> pd.Series:
    """Rolling OLS slope of y on x (fallback method, ТЗ §4.1)."""
    x = x.astype(float)
    y = y.astype(float)
    cov = y.rolling(window).cov(x)
    var = x.rolling(window).var(ddof=1)
    out = cov / var
    return out.replace([np.inf, -np.inf], np.nan)


def kalman_beta(x: pd.Series, y: pd.Series, q: float = 1e-4,
                r: float = 1e-2, init_window: int = 10) -> np.ndarray:
    """Dynamic β_t from a 1-D state-space Kalman filter (ТЗ §4.1):

        β_t      = β_{t-1} + η_t,   η ~ N(0, q)      (random-walk state)
        ln(P1_t) = β_t·ln(P2_t) + ε_t,  ε ~ N(0, r)  (observation)

    β_t at bar t uses only bars <= t, so the resulting spread is honest
    point-in-time. q/r sets how fast β adapts (defaults ≈ 30-40 bar
    timescale on ln-price magnitudes; both are configurable)."""
    xv = x.to_numpy(dtype=float)
    yv = y.to_numpy(dtype=float)
    n = len(xv)
    if n == 0:
        return np.array([])
    beta = np.empty(n)
    P = 1.0
    # init from a short OLS fit so the filter starts near the true level
    m = min(init_window, n)
    if m >= 2 and np.ptp(xv[:m]) > 1e-12:
        b = float(np.polyfit(xv[:m], yv[:m], 1)[0])
    else:
        b = 0.0
    for t in range(n):
        xt = xv[t]
        P = P + q                                  # predict
        if np.isfinite(yv[t]) and np.isfinite(xt):
            k = P * xt / (P * xt * xt + r)         # Kalman gain
            b = b + k * (yv[t] - xt * b)           # update
            P = (1.0 - k * xt) * P
        beta[t] = b
    return beta


# ---------------------------------------------------------------------------
# Spread / z-score
# ---------------------------------------------------------------------------

def spread(y: pd.Series, x: pd.Series, beta) -> pd.Series:
    """Log-spread e_t = ln(P1_t) − β_t·ln(P2_t). `beta` may be a scalar or a
    series aligned with x."""
    b = beta if np.isscalar(beta) else pd.Series(beta, index=x.index)
    return (y - b * x).astype(float)


def zscore(e: pd.Series, window: int = 90) -> pd.Series:
    """z_t = (e_t − μ_e) / σ_e over the rolling window (sample std, ddof=1)."""
    mu = e.rolling(window).mean()
    sd = e.rolling(window).std(ddof=1)
    return ((e - mu) / sd).replace([np.inf, -np.inf], np.nan)


def adf_pvalue(e: pd.Series) -> float:
    """ADF test on the spread (ТЗ §4.1): p < 0.05 → cointegration confirmed."""
    s = e.dropna().to_numpy(dtype=float)
    if len(s) < 20:
        return float("nan")
    try:
        res = adfuller(s, autolag="AIC")
        return float(res[1])
    except (ValueError, FloatingPointError):
        return float("nan")


def half_life(e: pd.Series) -> tuple[float, float]:
    """OU half-life (ТЗ §4.1): regress Δe_t = α − θ·e_{t-1} + ε on the
    spread, HL = ln(2)/θ. Returns (θ, hl_bars); θ <= 0 (no reversion) yields
    hl_bars = +inf."""
    s = e.dropna().to_numpy(dtype=float)
    if len(s) < 10:
        return float("nan"), float("nan")
    de = np.diff(s)
    lag = s[:-1]
    A = np.column_stack([np.ones(len(lag)), lag])
    coef, *_ = np.linalg.lstsq(A, de, rcond=None)
    theta = -float(coef[1])
    if not np.isfinite(theta) or theta <= 0:
        return theta, float("inf")
    return theta, float(np.log(2.0) / theta)


def spread_sigma(e: pd.Series, window: int = 90) -> float:
    """Current spread volatility σ (rolling std of e_t, last value)."""
    s = e.rolling(window).std(ddof=1)
    v = s.dropna()
    return float(v.iloc[-1]) if len(v) else float("nan")


def annualized_sigma(e: pd.Series, bars_per_year: float, window: int = 90) -> float:
    """σ in annualized form for display (ТЗ §4.1)."""
    s = spread_sigma(e, window)
    if not np.isfinite(s):
        return float("nan")
    return s * float(np.sqrt(max(bars_per_year, 1.0)))


# ---------------------------------------------------------------------------
# Math Board stats (ТЗ §4.2) — stage-1 core subset
# ---------------------------------------------------------------------------

def hurst_rs(x: pd.Series, max_lag: int = 100) -> float:
    """Hurst exponent by the R/S method: E[R(n)/S(n)] = C·n^H.

    Feed the series of RETURNS (diffs of the spread/price), not the levels:
    plain R/S on levels is biased by short-memory artefacts (Lo 1991) and
    mislabels mean-reverting series as trending. On returns the mapping is
    the one the ТЗ wants: H < 0.5 → mean-reverting, H ≈ 0.5 → random walk,
    H > 0.5 → persistent/trending regime.

    Optimized: vectorized segment processing, reduced lag set for large n."""
    s = x.dropna().to_numpy(dtype=float)
    n = len(s)
    if n < 20:
        return float("nan")
    # For large n, use fewer lags (log-spaced) — the fit only needs ~20 points
    if n > 5000:
        lag_set = np.unique(np.logspace(0.3, np.log10(n // 2), 30, dtype=int))
        lag_set = lag_set[(lag_set >= 2) & (lag_set <= n // 2)]
    else:
        lag_set = np.arange(2, min(max_lag, n // 2))
    rs_vals, lag_list = [], []
    for lag in lag_set:
        segs = n // lag
        if segs < 1:
            continue
        # Vectorized: reshape into (segs, lag) matrix
        trim = segs * lag
        mat = s[:trim].reshape(segs, lag)
        seg_means = mat.mean(axis=1, keepdims=True)
        seg_stds = mat.std(axis=1, ddof=1)
        # Mask segments with zero std
        valid = seg_stds > 1e-12
        if not np.any(valid):
            continue
        devs = np.cumsum(mat - seg_means, axis=1)
        R = devs.max(axis=1) - devs.min(axis=1)
        rs_valid = R[valid] / seg_stds[valid]
        if len(rs_valid) > 0:
            rs_vals.append(float(np.mean(rs_valid)))
            lag_list.append(int(lag))
    if len(lag_list) < 4:
        return float("nan")
    slope, *_ = np.polyfit(np.log(np.array(lag_list, dtype=float)),
                           np.log(np.array(rs_vals, dtype=float)), 1)
    return float(slope)


def skew(x: pd.Series) -> float:
    s = x.dropna().to_numpy(dtype=float)
    if len(s) < 8:
        return float("nan")
    return float(stats.skew(s))


def excess_kurtosis(x: pd.Series) -> float:
    s = x.dropna().to_numpy(dtype=float)
    if len(s) < 8:
        return float("nan")
    return float(stats.kurtosis(s))          # scipy default = excess kurtosis


def acf1(x: pd.Series) -> float:
    """Autocorrelation of the spread returns at lag 1."""
    s = x.dropna().to_numpy(dtype=float)
    if len(s) < 4:
        return float("nan")
    r = np.diff(s)
    if r.std(ddof=1) < 1e-12:
        return float("nan")
    return float(np.corrcoef(r[:-1], r[1:])[0, 1])


def realized_vol_pct(x: pd.Series, window: int = 90) -> float:
    """Realized volatility of spread returns in % over the window (ТЗ §4.2)."""
    s = x.dropna().to_numpy(dtype=float)
    if len(s) < window + 2:
        return float("nan")
    r = np.diff(s[-(window + 1):])
    return float(r.std(ddof=1) * 100.0)
