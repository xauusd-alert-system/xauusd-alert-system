"""
Purged K-Fold CV with embargo (Lopez de Prado, "Advances in Financial ML",
ch. 7) — the time-series CV used by feature selection and meta-prechecks.

A standard KFold on overlapping-label time series leaks: a train row whose
label window extends into the test period contains test information. The
purge removes train rows whose label window overlaps the test window; the
embargo additionally drops test-adjacent train rows to account for serial
correlation of features.

Label windows are defined by `horizon` (labeling.horizon_candles_n): a row
at position i "touches" rows i+1 .. i+horizon. When a train row's window
reaches into the test block, it is purged.
"""
from __future__ import annotations

import numpy as np


def purge_train_indices(train_idx: np.ndarray, test_start: int, test_end: int,
                        horizon: int) -> np.ndarray:
    """Drop train rows whose label window [i+1, i+horizon] overlaps the test
    block [test_start, test_end). Time-indexed positions (0..n-1)."""
    keep = []
    for i in train_idx:
        win_start = i + 1
        win_end = i + horizon + 1  # exclusive
        if win_end <= test_start or win_start >= test_end:
            keep.append(i)
    return np.asarray(keep, dtype=int)


def embargo_train_indices(train_idx: np.ndarray, test_start: int,
                          embargo: int) -> np.ndarray:
    """Drop train rows within `embargo` positions BEFORE the test block
    (serial-correlation buffer)."""
    keep = [i for i in train_idx if i < test_start - embargo]
    return np.asarray(keep, dtype=int)


def purged_kfold_indices(n: int, n_splits: int, horizon: int,
                         embargo: int = 0) -> list[tuple[np.ndarray, np.ndarray]]:
    """Time-ordered purged K-fold: returns [(train_idx, test_idx), ...] with
    contiguous test blocks, purged + embargoed train blocks.

    Test blocks are contiguous slices of the series (positions), split
    sequentially — the standard "purged K-fold" over time. n_splits <= n.
    """
    n = int(n)
    n_splits = max(2, min(int(n_splits), n))
    horizon = max(1, int(horizon))
    embargo = max(0, int(embargo))
    all_idx = np.arange(n)
    # Sequential blocks; last block absorbs the remainder.
    edges = np.linspace(0, n, n_splits + 1).astype(int)
    folds = []
    for k in range(n_splits):
        test_start = edges[k]
        test_end = edges[k + 1]
        test_idx = all_idx[test_start:test_end]
        if len(test_idx) == 0:
            continue
        train = np.concatenate([all_idx[:test_start], all_idx[test_end:]])
        train = purge_train_indices(train, test_start, test_end, horizon)
        if embargo > 0:
            train = embargo_train_indices(train, test_start, embargo)
        folds.append((train, test_idx))
    return folds
