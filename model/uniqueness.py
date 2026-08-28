"""
Average uniqueness of overlapping labels and trades (Lopez de Prado, AFML ch. 4).

With horizon-overlapping labels and trades, each observation appears in
multiple label/holding windows; giving every row the same weight over-represents
the information that is actually unique. `average_uniqueness_weights` computes,
for each row, 1/count of labels covering it averaged over the rows that share its
window — the standard weighting used to make purged CV honest and to weight
training rows by information content (audit question 2: "Sample weights по
уникальности метки при перекрывающихся горизонтах").

`compute_event_uniqueness` and `compute_trade_uniqueness` compute the average
uniqueness bar-by-bar across arbitrary overlapping time intervals [t_0, t_1],
yielding an effective sample size T_eff = sum(uniqueness) used in DSR and PBO
instead of nominal trade count N.
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


def aligned_uniqueness_weights(
    source_index: pd.Index,
    selected_index: pd.Index,
    horizon: int,
    default: float | None = None,
) -> np.ndarray:
    """Compute uniqueness on the full chronological frame and align it to X rows.

    Feature/label preparation drops warm-up, unresolved and ambiguous rows.  Weight
    arrays therefore cannot safely be sliced by length: they must be keyed by the
    original frame index.  Missing indices are an error by default because silently
    assigning unit weight recreates the research/production asymmetry this helper is
    intended to remove.
    """
    source_index = pd.Index(source_index)
    selected_index = pd.Index(selected_index)
    if not source_index.is_unique:
        raise ValueError("source_index must be unique for sample-weight alignment")

    series = pd.Series(average_uniqueness_weights(len(source_index), horizon), index=source_index)
    aligned = series.reindex(selected_index)
    if aligned.isna().any():
        missing = selected_index[aligned.isna()]
        if default is None:
            raise ValueError(
                f"sample-weight alignment failed for {len(missing)} rows; first missing index={missing[0]!r}"
            )
        aligned = aligned.fillna(float(default))

    values = aligned.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("sample weights must be finite and non-negative")
    if len(values) and not (values > 0).any():
        raise ValueError("all aligned sample weights are zero")
    return values


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


def compute_event_uniqueness(
    event_spans: list[tuple[int, int]] | np.ndarray,
    min_t: int | None = None,
    max_t: int | None = None,
) -> np.ndarray:
    """Computes average uniqueness per event according to López de Prado (AFML ch. 4).

    Parameters
    ----------
    event_spans : list of (t_start, t_end) inclusive integer timestamps/indices.
    min_t, max_t : optional coordinate bounds.

    Returns
    -------
    uniqueness : 1D np.ndarray of shape (N,) with average uniqueness in (0, 1].
                 The effective sample size is T_eff = np.sum(uniqueness).
    """
    if len(event_spans) == 0:
        return np.array([], dtype=float)

    spans = [
        (int(min(s[0], s[1])), int(max(s[0], s[1])))
        for s in event_spans
        if s is not None and not (isinstance(s[0], float) and np.isnan(s[0]))
    ]
    n_events = len(spans)
    if n_events == 0:
        return np.array([], dtype=float)

    # Offset to 0-based indexing for efficient array manipulation
    lo = min(s[0] for s in spans) if min_t is None else int(min_t)
    hi = max(s[1] for s in spans) if max_t is None else int(max_t)
    span_len = max(1, hi - lo + 1)

    # Concurrency count c_t at each discrete point
    coverage = np.zeros(span_len, dtype=float)
    for s_start, s_end in spans:
        idx_start = max(0, s_start - lo)
        idx_end = min(span_len - 1, s_end - lo)
        coverage[idx_start : idx_end + 1] += 1.0

    # Uniqueness u_{i, t} = 1 / c_t averaged over the event's duration
    uniqueness = np.zeros(n_events, dtype=float)
    for i, (s_start, s_end) in enumerate(spans):
        idx_start = max(0, s_start - lo)
        idx_end = min(span_len - 1, s_end - lo)
        cov_slice = coverage[idx_start : idx_end + 1]
        valid = cov_slice > 0
        if valid.any():
            uniqueness[i] = float(np.mean(1.0 / cov_slice[valid]))
        else:
            uniqueness[i] = 1.0

    return uniqueness


def compute_trade_uniqueness(
    trades: pd.DataFrame | list,
    horizon_bars: int | None = None,
) -> np.ndarray:
    """Computes average uniqueness per trade and effective sample size T_eff.

    Accepts a DataFrame with `entry_ts` and `exit_ts` (or `entry_bar` and `exit_bar`),
    or a list of Trade dataclass objects.
    If timestamps are missing, falls back to consecutive windows of `horizon_bars`.

    Returns
    -------
    uniqueness : 1D array of weights in (0, 1], where np.sum(uniqueness) = T_eff.
    """
    if trades is None or (isinstance(trades, (list, tuple, np.ndarray)) and len(trades) == 0):
        return np.array([], dtype=float)
    if isinstance(trades, pd.DataFrame) and len(trades) == 0:
        return np.array([], dtype=float)

    # DataFrame input
    if isinstance(trades, pd.DataFrame):
        tdf = trades
        if "entry_ts" in tdf.columns and "exit_ts" in tdf.columns:
            valid = tdf.dropna(subset=["entry_ts", "exit_ts"])
            if len(valid) == len(tdf):
                # Discretize timestamps to bar-like steps if large
                e_ts = tdf["entry_ts"].values.astype(int)
                x_ts = tdf["exit_ts"].values.astype(int)
                diffs = np.diff(np.sort(e_ts))
                step = int(np.median(diffs[diffs > 0])) if (diffs > 0).any() else 1
                spans = [(int(e // step), int(x // step)) for e, x in zip(e_ts, x_ts)]
                return compute_event_uniqueness(spans)
        # Sequential fallback if horizon_bars is provided
        n = len(tdf)
        h = horizon_bars or 1
        return average_uniqueness_weights(n, h)

    # List of trade objects
    if isinstance(trades, list):
        if len(trades) > 0 and hasattr(trades[0], "entry_ts"):
            e_ts = [getattr(t, "entry_ts", 0) for t in trades]
            x_ts = [getattr(t, "exit_ts", None) or getattr(t, "entry_ts", 0) for t in trades]
            diffs = np.diff(np.sort(e_ts))
            step = int(np.median(diffs[diffs > 0])) if (diffs > 0).any() else 1
            spans = [(int(e // step), int(x // step)) for e, x in zip(e_ts, x_ts)]
            return compute_event_uniqueness(spans)
        n = len(trades)
        h = horizon_bars or 1
        return average_uniqueness_weights(n, h)

    return np.array([], dtype=float)
