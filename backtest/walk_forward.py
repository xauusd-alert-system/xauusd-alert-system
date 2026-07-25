"""
Walk-forward validation: rolling train/test windows over time, re-evaluating the
strategy on each out-of-sample test window without ever training on future data
relative to that window.

For the rule-based baseline (Step 7), there is no "training" step per se, but the
walk-forward harness is built now so it works identically once model/ (Step 8+)
introduces an actual trained model that MUST be fit only on the train window and
evaluated only on the immediately following test window - never on data that
overlaps or precedes what was used for fitting.
"""
import pandas as pd
from typing import Callable, List, Dict
from dataclasses import dataclass


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

    results = []
    for w in windows:
        train_df = df[(df["timestamp_utc"] >= w.train_start_ts) & (df["timestamp_utc"] < w.train_end_ts)]
        test_df = df[(df["timestamp_utc"] >= w.test_start_ts) & (df["timestamp_utc"] < w.test_end_ts)]
        if len(test_df) == 0:
            continue
        fold_result = strategy_fn(train_df, test_df, cfg)
        fold_result["window"] = w
        results.append(fold_result)

    return results
