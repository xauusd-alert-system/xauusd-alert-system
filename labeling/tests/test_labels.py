"""
Unit tests for labeling/label_generator.py.
Validates label correctness on constructed synthetic price paths where the
correct outcome is known exactly, plus statistical sanity checks on distribution.
Run with: pytest labeling/tests/test_labels.py -v
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import fetch_mock_candles
from labeling.label_generator import (
    generate_labels,
    generate_labels_from_config,
    label_distribution_summary,
)

CFG = load_config()
SESSIONS = CFG["sessions"]


# Override labeling barriers to match mock data scale (~0.15 pts/candle drift).
# These unit tests exercise the FIXED-barrier path: mock candles carry no "atr"
# column, and build_all_indicators() is out of scope here (covered in test_trainer).
import copy

CFG = copy.deepcopy(CFG)
CFG["labeling"]["method"] = "fixed"
CFG["labeling"]["target_pips_x"] = 3.0
CFG["labeling"]["stop_pips_y"] = 2.0


def _flat_df(n, price=2000.0):
    return pd.DataFrame(
        {
            "timestamp_utc": np.arange(n) * 900,
            "open": np.full(n, price),
            "high": np.full(n, price),
            "low": np.full(n, price),
            "close": np.full(n, price),
            "volume": np.full(n, 100.0),
        }
    )


def test_upper_barrier_hit_produces_label_1():
    """Construct a path where price rises past +X well before -Y or N expires."""
    n = 20
    df = _flat_df(n)
    # At candle 5, price spikes up past the upper barrier
    df.loc[5, "high"] = 2000.0 + 200.0  # target_x default in config is 150
    labels = generate_labels(df, target_x=150, stop_y=100, horizon_n=10)
    assert labels.iloc[0] == 1


def test_lower_barrier_hit_produces_label_minus_1():
    """Construct a path where price falls past -Y before hitting +X."""
    n = 20
    df = _flat_df(n)
    df.loc[3, "low"] = 2000.0 - 120.0  # stop_y = 100
    labels = generate_labels(df, target_x=150, stop_y=100, horizon_n=10)
    assert labels.iloc[0] == -1


def test_no_hit_within_horizon_produces_label_0():
    """Flat price series never touches either barrier -> label 0 (time barrier expiry)."""
    n = 20
    df = _flat_df(n)
    labels = generate_labels(df, target_x=150, stop_y=100, horizon_n=10)
    assert labels.iloc[0] == 0


def test_insufficient_future_data_produces_nan():
    """Rows near the end of the dataset (fewer than horizon_n future candles) must be NaN."""
    n = 20
    df = _flat_df(n)
    horizon = 10
    labels = generate_labels(df, target_x=150, stop_y=100, horizon_n=horizon)
    # Last `horizon` rows must all be NaN since we cannot look beyond the dataset
    assert labels.iloc[-horizon:].isna().all()
    # Rows before that with enough future data must NOT be NaN
    assert not labels.iloc[: n - horizon].isna().any()


def test_double_touch_same_candle_is_excluded_not_short():
    """
    If a single future candle's range covers BOTH barriers (huge volatility candle),
    intra-candle order is unknowable from OHLC alone. Regression (quant audit
    2026-08-07): this used to be hard-coded to -1 (short), a systematic
    directional bias in training labels. The ambiguous observation must now be
    EXCLUDED (NaN), never labeled.
    """
    n = 20
    df = _flat_df(n)
    df.loc[4, "high"] = 2000.0 + 200.0
    df.loc[4, "low"] = 2000.0 - 200.0
    labels = generate_labels(df, target_x=150, stop_y=100, horizon_n=10)
    assert np.isnan(labels.iloc[0])
    # and no other row may be polluted by the ambiguous bar's direction
    assert not (labels.iloc[1:10] == -1).all()


def test_double_touch_atr_scaled_is_excluded_too():
    """Same exclusion contract for the ATR-scaled label path."""
    n = 20
    df = _flat_df(n)
    df["atr"] = 100.0
    df.loc[4, "high"] = 2000.0 + 200.0
    df.loc[4, "low"] = 2000.0 - 200.0
    from labeling.label_generator import generate_labels_atr_scaled

    labels = generate_labels_atr_scaled(
        df, target_atr_multiplier=1.5, stop_atr_multiplier=1.0, horizon_n=10, atr_col="atr"
    )
    assert np.isnan(labels.iloc[0])


def test_ambiguous_bar_excluded_from_training_matrix():
    """build_training_matrix drops NaN labels, so excluded observations never
    reach the model (both in binary and 3-class mode)."""
    from model.trainer import build_training_matrix

    n = 20
    df = _flat_df(n)
    df.loc[4, "high"] = 2000.0 + 200.0
    df.loc[4, "low"] = 2000.0 - 200.0
    labels = generate_labels(df, target_x=150, stop_y=100, horizon_n=10)
    df["label"] = labels
    # Minimal feature columns the trainer requires
    df["regime"] = "trend_up"
    df["rsi"] = 50.0
    df["atr"] = 100.0
    X, y, cols = build_training_matrix(df, cfg={"model": {"include_zero_class": False}})
    # The ambiguous row must not be part of training
    assert len(y) == 0 or not np.isnan(y).any()


def test_label_distribution_on_mock_data_is_not_degenerate():
    """
    Statistical validation: on a reasonably long mock M15 series, the label
    distribution should not be 100% one class (which would indicate a bug or a
    misconfigured X/Y that makes one outcome nearly impossible).
    """
    df = fetch_mock_candles("M15", n_candles=1000, sessions_config=SESSIONS)
    labels = generate_labels_from_config(df, CFG)
    summary = label_distribution_summary(labels)
    assert summary["total_valid"] > 0
    # No single outcome class should dominate 100% - some variation expected even in mock data
    assert summary["pct_no_hit"] < 100.0 or summary["total_valid"] < 5


def test_generate_labels_from_config_uses_config_values():
    """Sanity check that the config wrapper actually threads X/Y/N from config.yaml."""
    df = fetch_mock_candles("M15", n_candles=100, sessions_config=SESSIONS)
    direct = generate_labels(
        df,
        target_x=CFG["labeling"]["target_pips_x"],
        stop_y=CFG["labeling"]["stop_pips_y"],
        horizon_n=CFG["labeling"]["horizon_candles_n"],
    )
    via_config = generate_labels_from_config(df, CFG)
    pd.testing.assert_series_equal(direct, via_config)
