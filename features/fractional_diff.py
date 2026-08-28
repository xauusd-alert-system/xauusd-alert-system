"""
Fractional differentiation (Lopez de Prado, "Advances in Financial ML", ch. 5).

A cheap capacity boost over the 46 engineered features (audit question 2,
"what instead of DL"): apply the minimum fractional differentiation d that
keeps the series stationary (ADF test) while preserving the maximum memory
(fraction of variance). At d=0 the series is the raw price (non-stationary);
at d=1 it is the first difference (memory destroyed). Fractional d keeps the
long-memory component the ML model can use.

This module provides:
- `get_weights_ffd(d, thresh)` — the FFD (fixed-width window) binomial weights.
- `frac_diff(series, d, thresh)` — FFD differencing with weight truncation
  (expanding window up to the truncated weight length).
- `frac_diff_fdf(series, d, window)` — FDF variant: fractional differencing
  over a FIXED-width window of raw past observations (strictly bounded
  lookback, weights recomputed per window length).
- `min_d_adf(series, d_list)` — smallest d that passes the ADF test at the
  given significance level (the audit's "minimum d such that the series
  passes ADF, keeping maximum memory").

Behavior contract (documented, tested):
- Empty series -> empty series (no error).
- d == 0 -> identity (NaN only for missing-weights warm-up, none for d=0
  because wlen == 1).
- d == 1 -> the standard first difference (single lag), no warm-up NaN.
- d < 0 or d > 1 -> ValueError (outside the meaningful memory/stationarity
  trade-off range).
- NaN/inf inputs propagate: rows whose window touches NaN/inf produce NaN
  (np.dot with non-finite values), never silent zero. NaNs inside the
  warm-up region remain NaN. Callers should dropna() as with any warm-up.

The feature is NOT enabled by default (config `features.fractional_diff` is
off); it is a research tool to be evaluated per-asset with the feature-
selection + walk-forward pipeline (compare E[R]/AUC with and without).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_D_LIST = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def get_weights_ffd(d: float, thres: float = 1e-5) -> np.ndarray:
    """FFD binomial weights of (1 - L)^d with magnitude truncation.

    Weights follow the expansion of (1 - L)^d:
        w_k = -w_{k-1} * (d - k + 1) / k,  w_0 = 1
    Weights are returned OLDEST-FIRST (w_K ... w_0 reversed) so a dot product
    against an ascending window directly yields the differenced value.
    Iteration stops when |w_k| < thres * max(|w|) (thres > 0), or at k=1 when
    d <= 0 makes the weight sequence terminate naturally.
    """
    if not np.isfinite(d):
        raise ValueError(f"d must be finite, got {d!r}")
    if d < 0.0 or d > 1.0:
        raise ValueError(f"d must be within [0, 1], got {d!r}")
    if thres <= 0 or not np.isfinite(thres):
        raise ValueError(f"thres must be positive and finite, got {thres!r}")

    weights: list[float] = [1.0]
    k = 1
    w_prev = 1.0
    w_max = 1.0
    while True:
        w_k = -w_prev * (d - k + 1) / k
        w_max = max(w_max, abs(w_k))
        # Break on truncation, or on an exact zero (d == 0 terminates at k=1).
        if abs(w_k) < thres * w_max and (k > 1 or w_k == 0.0):
            break
        weights.append(w_k)
        w_prev = w_k
        k += 1
        if k > 10000:  # hard numeric safety bound (thres never triggers for d=1)
            break
    arr = np.asarray(weights[::-1], dtype=float)  # oldest first
    # Exact zeros at the OLDEST end (e.g. d=0 appends w_1=0 before the loop
    # breaks) contribute nothing; trim them so d=0 is the clean identity [1].
    while len(arr) > 1 and arr[0] == 0.0:
        arr = arr[1:]
    return arr


def _validate_series(series: pd.Series) -> pd.Series:
    """Common input guards: type and finiteness of the container."""
    if not isinstance(series, pd.Series):
        raise TypeError(f"expected pandas.Series, got {type(series)!r}")
    x = series.astype(float)
    if x.empty:
        return x
    return x


def frac_diff(series: pd.Series, d: float, thresh: float = 1e-5) -> pd.Series:
    """Fractionally differentiate `series` (FFD with weight truncation).

    Weights follow the binomial expansion of (1 - L)^d; see get_weights_ffd.
    Weights with |w_k| < thresh * max(|w|) are dropped (keeps the computation
    O(weight_len) instead of O(n^2)). The first `weight_len - 1` rows are NaN
    (no full history), matching the label-generator convention of dropping
    warm-up rows. Windows touching NaN/inf inputs produce NaN values.
    """
    x = _validate_series(series)
    if x.empty:
        return pd.Series(np.nan, index=series.index, name=f"{series.name}_fd{d}")
    if not np.isfinite(d):
        raise ValueError(f"d must be finite, got {d!r}")
    if d < 0.0 or d > 1.0:
        raise ValueError(f"d must be within [0, 1], got {d!r}")

    weights = get_weights_ffd(d, thres=thresh)  # oldest first
    wlen = len(weights)
    values = x.to_numpy(dtype=float)

    out = np.full(len(x), np.nan)
    if wlen > len(x):
        # Not enough history for even one full window: all NaN.
        return pd.Series(out, index=series.index, name=f"{series.name}_fd{d}")

    for i in range(wlen - 1, len(x)):
        window = values[i - wlen + 1 : i + 1]
        out[i] = float(np.dot(weights, window))
    return pd.Series(out, index=series.index, name=f"{series.name}_fd{d}")


def frac_diff_fdf(series: pd.Series, d: float, window: int) -> pd.Series:
    """FDF variant: fractional differencing over a FIXED-width window.

    Unlike frac_diff (expanding window up to the truncated weight length),
    this variant uses exactly `window` most-recent raw observations for every
    output row, applying the FFD weights truncated/expanded to that width.
    Guarantees a strictly bounded lookback regardless of d and thresh, which
    is useful when a hard max-lookback is required (e.g. mirroring the live
    inference window).

    The first `window - 1` rows are NaN (warm-up). Windows touching NaN/inf
    inputs produce NaN values.

    NOTE: with window < the natural truncated weight length, the weight
    sequence is cut from the OLDEST end (the tail of small weights), so the
    output approximates the frac_diff series with a controlled bias; with
    window >= natural length the two coincide.
    """
    x = _validate_series(series)
    if not np.isfinite(d):
        raise ValueError(f"d must be finite, got {d!r}")
    if d < 0.0 or d > 1.0:
        raise ValueError(f"d must be within [0, 1], got {d!r}")
    if not isinstance(window, (int, np.integer)) or window < 1:
        raise ValueError(f"window must be a positive integer, got {window!r}")

    out = np.full(len(x), np.nan)
    if x.empty or window > len(x):
        return pd.Series(out, index=series.index, name=f"{series.name}_fdf{d}_{window}")

    if d == 0.0:
        # Identity: only the window's last value contributes (w_0 = 1).
        out[window - 1 :] = x.to_numpy(dtype=float)[window - 1 :]
        return pd.Series(out, index=series.index, name=f"{series.name}_fdf{d}_{window}")

    values = x.to_numpy(dtype=float)
    for i in range(window - 1, len(x)):
        w = get_weights_ffd(d, thres=0.0 if window == 1 else 1e-5)
        # Trim or pad the weight vector to exactly `window` entries, oldest first.
        if len(w) > window:
            w = w[-window:]  # keep the newest-end weights, drop small old tail
        elif len(w) < window:
            pad = np.zeros(window - len(w))
            w = np.concatenate([pad, w])
        out[i] = float(np.dot(w, values[i - window + 1 : i + 1]))
    return pd.Series(out, index=series.index, name=f"{series.name}_fdf{d}_{window}")


def min_d_adf(series: pd.Series, d_list: list[float] | None = None, significance: float = 0.05) -> dict:
    """Smallest d in d_list whose fractionally-differenced series passes the
    ADF test at `significance` (i.e. is stationary). Returns the chosen d and
    the ADF statistic/p-value ladder."""
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
        ladder.append(
            {
                "d": d,
                "adf_stat": float(stat) if stat is not None else None,
                "p_value": float(p) if p is not None else None,
                "stationary": stationary,
            }
        )
        if stationary and chosen is None:
            chosen = d
    return {"min_d": chosen, "ladder": ladder}
