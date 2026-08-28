"""
Walk-forward validation: rolling train/test windows over time, re-evaluating the
strategy on each out-of-sample test window without ever training on future data
relative to that window.

For the rule-based baseline (Step 7), there is no "training" step per se, but the
walk-forward harness is built now so it works identically once model/ (Step 8+)
introduces an actual trained model that MUST be fit only on the train window and
evaluated only on the immediately following test window - never on data that
overlaps or precedes what was used for fitting.

Slicing lives in ONE place. `split_fold_frames` is the only sanctioned way to
turn a window into (train, test) frames: it applies the label-horizon purge and
the embargo. Research harnesses (scripts/deflated_sharpe.py, scripts/diag_*.py)
must call it instead of re-deriving the boundaries, because a harness that skips
the purge trains on rows whose labels resolve inside its own test window and
then reports the result as out-of-sample.
"""
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class WalkForwardWindow:
    train_start_ts: int
    train_end_ts: int
    test_start_ts: int
    test_end_ts: int


def generate_windows(df: pd.DataFrame, train_window_days: int, test_window_days: int,
                      step_days: int) -> List[WalkForwardWindow]:
    """
    Generate non-overlapping (in test) rolling windows across the full timestamp range.
    Each test window strictly follows its train window in time - train_end_ts <= test_start_ts,
    guaranteeing no test-window information can leak backward into training.
    """
    if len(df) == 0:
        return []

    min_ts = int(df["timestamp_utc"].min())
    max_ts = int(df["timestamp_utc"].max())
    day_s = 86400

    windows = []
    train_start = min_ts
    while True:
        train_end = train_start + train_window_days * day_s
        test_start = train_end
        test_end = test_start + test_window_days * day_s
        if test_end > max_ts:
            break
        windows.append(WalkForwardWindow(train_start, train_end, test_start, test_end))
        train_start += step_days * day_s

    return windows


def bar_seconds(df: pd.DataFrame) -> int:
    """Median positive bar spacing of the frame, in seconds.

    The purge distance has to be expressed in TIME, not in row counts, because a
    frame with weekend gaps has no constant rows-per-day. Median (not mode) keeps
    a handful of gaps from moving the estimate.
    """
    if "timestamp_utc" not in df.columns or len(df) < 2:
        return 1
    diffs = np.diff(df["timestamp_utc"].values)
    diffs = diffs[diffs > 0]
    return int(np.median(diffs)) if len(diffs) else 1


def purge_train_frame(train_df: pd.DataFrame, test_start_ts: int, cfg: dict,
                      bar_secs: int) -> pd.DataFrame:
    """Drop the tail of a train window whose labels reach into the test window.

    W3 (audit 2026-08-10): triple-barrier labels of the LAST `horizon` bars of a
    train window resolve INSIDE the following test window (test_start == train_end),
    so those train rows leak future information. We purge them plus an optional
    extra embargo buffer for serial feature correlation. This makes the effective
    training set honest and, together with sample weights, keeps DSR/PBO/
    block-bootstrap-t from being computed on an inflated effective N.

    `horizon` comes from `labeling.horizon_candles_n` and `embargo` from
    `backtest.walk_forward.embargo_candles`, so a caller that merged a per-asset
    labeling section gets that asset's horizon.
    """
    horizon = int(cfg.get("labeling", {}).get("horizon_candles_n", 0))
    embargo = int(cfg.get("backtest", {}).get("walk_forward", {}).get("embargo_candles", 0))
    if horizon <= 0 or len(train_df) == 0:
        return train_df
    purge_cutoff = int(test_start_ts) - horizon * int(bar_secs)
    out = train_df[train_df["timestamp_utc"] <= purge_cutoff]
    if embargo > 0:
        out = out[out["timestamp_utc"] <= purge_cutoff - embargo * int(bar_secs)]
    return out


def split_fold_frames(df: pd.DataFrame, cfg: dict, window: WalkForwardWindow,
                       bar_secs: Optional[int] = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Return (purged_train_df, test_df) for one walk-forward window.

    THE shared slicing path. The train frame is positionally re-indexed because
    uniqueness sample weights are computed over the surviving train rows and must
    align with the rows build_training_matrix keeps. The test frame keeps its
    original index so callers can write predictions back into a copy of it.
    """
    if bar_secs is None:
        bar_secs = bar_seconds(df)
    train_df = df[(df["timestamp_utc"] >= window.train_start_ts) &
                  (df["timestamp_utc"] < window.train_end_ts)]
    test_df = df[(df["timestamp_utc"] >= window.test_start_ts) &
                 (df["timestamp_utc"] < window.test_end_ts)]
    train_df = purge_train_frame(train_df, window.test_start_ts, cfg, bar_secs)
    return train_df.reset_index(drop=True), test_df


def run_walk_forward(df: pd.DataFrame, cfg: dict,
                      strategy_fn: Callable[[pd.DataFrame, pd.DataFrame, dict], Dict]) -> List[dict]:
    """
    strategy_fn(train_df, test_df, cfg) -> metrics dict for that fold.
    For the rule-based baseline, train_df is unused (no fitting needed) but is still
    passed for interface consistency with the future ML strategy_fn.
    Returns a list of per-fold results, each tagged with its window boundaries.
    """
    wf_cfg = cfg["backtest"]["walk_forward"]
    windows = generate_windows(df, wf_cfg["train_window_days"], wf_cfg["test_window_days"], wf_cfg["step_days"])
    bar_secs = bar_seconds(df)

    results = []
    for w in windows:
        train_df, test_df = split_fold_frames(df, cfg, w, bar_secs=bar_secs)
        if len(test_df) == 0:
            continue
        fold_result = strategy_fn(train_df, test_df, cfg)
        fold_result["window"] = w
        fold_result["purged_train_rows"] = int(len(train_df))
        results.append(fold_result)

    return results
