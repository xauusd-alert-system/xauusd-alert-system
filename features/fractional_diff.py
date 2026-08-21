"""
Fractional differentiation (Lopez de Prado, "Advances in Financial ML", ch. 5).

A cheap capacity boost over the 46 engineered features (audit question 2,
"what instead of DL"): apply the minimum fractional differentiation d that
keeps the series stationary (ADF test) while preserving the maximum memory
(fraction of variance). At d=0 the series is the raw price (non-stationary);
at d=1 it is the first difference (memory destroyed). Fractional d keeps the
long-memory component the ML model can use.

This module provides:
- `frac_diff(series, d, thresh)` — the standard binomial-weight fractional
  differencing with weight truncation.
- `min_d_adf(series, d_list)` — smallest d that passes the ADF test at the
  given significance level (the audit's "minimum d such that the series
  passes ADF, keeping maximum memory").

The feature is NOT enabled by default (config `features.fractional_diff` is
off); it is a research tool to be evaluated per-asset with the feature-
selection + walk-forward pipeline (compare E[R]/AUC with and without).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_D_LIST = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def frac_diff(series: pd.Series, d: float, thresh: float = 1e-5) -> pd.Series:
    """Fractionally differentiate `series` with weight truncation.

    Weights follow the binomial expansion of (1 - L)^d:
        w_k = -w_{k-1} * (d - k + 1) / k,  w_0 = 1
    Weights with |w_k| < thresh * max(|w|) are dropped (keeps the computation
    O(weight_len) instead of O(n^2)). The first `weight_len` rows are NaN
    (no full history), matching the label-generator convention of dropping
    warm-up rows.
    """
    x = series.astype(float)
    # build weights
    weights = [1.0]
    k = 1
    w_prev = 1.0
    w_max = 1.0
    while True:
        w_k = -w_prev * (d - k + 1) / k
        w_max = max(w_max, abs(w_k))
        if abs(w_k) < thresh * w_max and k > 1:
            break
        weights.append(w_k)
        w_prev = w_k
        k += 1
        if k > len(x):
            break
    weights = np.asarray(weights[::-1])  # oldest first
    wlen = len(weights)

    out = np.full(len(x), np.nan)
    for i in range(wlen - 1, len(x)):
        window = x.iloc[i - wlen + 1: i + 1].to_numpy(dtype=float)
        out[i] = float(np.dot(weights, window))
    return pd.Series(out, index=series.index, name=f"{series.name}_fd{d}")


def min_d_adf(series: pd.Series, d_list: list[float] | None = None,
              significance: float = 0.05) -> dict:
    """Smallest d in d_list whose fractionally-differenced series passes the
    ADF test at `significance` (i.e. is stationary). Returns the chosen d and
    the ADF statistic/p-value ladder."""
    from scipy import stats
    from statsmodels.tsa.stattools import adfuller

    d_list = d_list or DEFAULT_D_LIST
    x = series.astype(float).dropna()
    if len(x) < 30:
        return {"min_d": None, "ladder": []}
    ladder = []
    chosen = None
    for d in sorted(d_list):
        fd = frac_diff(x, d).dropna()
        if len(fd) < 30:
            ladder.append({"d": d, "adf_stat": None, "p_value": None, "stationary": False})
            continue
        try:
            stat, p, *_ = adfuller(fd, autolag="AIC")
        except Exception:
            stat, p = None, None
        stationary = bool(p is not None and p < significance)
        ladder.append({"d": d, "adf_stat": float(stat) if stat is not None else None,
                       "p_value": float(p) if p is not None else None,
                       "stationary": stationary})
        if stationary and chosen is None:
            chosen = d
    return {"min_d": chosen, "ladder": ladder}
