"""
Average uniqueness of overlapping labels (Lopez de Prado, ch. 4).

With horizon-overlapping labels, each observation appears in multiple label
windows; giving every row the same weight over-represents the information
that is actually unique. `average_uniqueness_weights` computes, for each
row, 1/count of labels covering it averaged over the rows that share its
window — the standard weighting used to make purged CV honest and to weight
training rows by information content (audit question 2: "Sample weights по
уникальности метки при перекрывающихся горизонтах").
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def average_uniqueness_weights(n: int, horizon: int) -> np.ndarray:
    """Weights by average label uniqueness for a series of length n with
    overlapping label windows of `horizon` bars.

    Row i's label uses bars i+1 .. i+horizon. coverage[t] = number of labels
    covering bar t. uniqueness(i) = mean over t in (i, i+horizon] of
    1/coverage[t]; rows with insufficient future (label NaN, i+horizon >= n)
    get weight 0 (they are dropped from training anyway).
    """
    n = int(n)
    horizon = max(1, int(horizon))
    coverage = np.zeros(n, dtype=float)
    for i in range(n):
        end = min(i + horizon, n - 1)
        for t in range(i + 1, end + 1):
            coverage[t] += 1.0
    weights = np.zeros(n, dtype=float)
    for i in range(n):
        end = min(i + horizon, n - 1)
        if i + horizon >= n:
            continue  # no full label window -> dropped by the trainer anyway
        span = np.arange(i + 1, end + 1)
        if len(span) == 0 or (coverage[span] <= 0).any():
            continue
        weights[i] = np.mean(1.0 / coverage[span])
    return weights


def sample_weight_series(df_len: int, horizon: int, decay_lambda: float = 0.0) -> np.ndarray:
    """Weights = average uniqueness, optionally multiplied by an exponential
    freshness decay exp(-lambda * age_in_days_like_units) — the audit's
    'sample_weight = exp(-λ·age)' for H1 assets with long windows.

    decay_lambda=0 (default) keeps pure uniqueness weights.
    """
    w = average_uniqueness_weights(df_len, horizon)
    if decay_lambda > 0.0:
        age = np.arange(df_len, dtype=float)
        w = w * np.exp(-decay_lambda * (age[-1] - age) / max(df_len, 1))
    return w
