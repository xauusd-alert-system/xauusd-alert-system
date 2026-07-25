"""
No-look-ahead proof tests for features/.

Core methodology: for each feature function, compute the feature on the FULL
dataset, then recompute it on a TRUNCATED dataset (rows [0..i] only). If the
value at row i differs between the two computations, the feature is using
future data at row i - this is exactly the bug class we must prevent.

Run with: pytest features/tests/test_no_lookahead.py -v
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import fetch_mock_candles
from features.indicators import ema, rsi, macd, atr, bollinger_width, build_all_indicators
from features.candle_anatomy import candle_anatomy
from features.structure import detect_structure
from features.mtf_confluence import merge_htf_feature

CFG = load_config()
SESSIONS = CFG["sessions"]


def _sample_df(n=300):
    return fetch_mock_candles("M5", n_candles=n, sessions_config=SESSIONS)


def test_ema_no_lookahead():
    df = _sample_df()
    i = 150
    full = ema(df["close"], 21)
    truncated = ema(df["close"].iloc[:i + 1], 21)
    assert np.isclose(full.iloc[i], truncated.iloc[i], rtol=1e-9)


def test_rsi_no_lookahead():
    df = _sample_df()
    i = 150
    full = rsi(df["close"], 14)
    truncated = rsi(df["close"].iloc[:i + 1], 14)
    assert np.isclose(full.iloc[i], truncated.iloc[i], rtol=1e-6)


def test_macd_no_lookahead():
    df = _sample_df()
    i = 150
    full = macd(df["close"], 12, 26, 9)
    truncated = macd(df["close"].iloc[:i + 1], 12, 26, 9)
    assert np.isclose(full["macd_line"].iloc[i], truncated["macd_line"].iloc[i], rtol=1e-9)


def test_atr_no_lookahead():
    df = _sample_df()
    i = 150
    full = atr(df, 14)
    truncated = atr(df.iloc[:i + 1], 14)
    assert np.isclose(full.iloc[i], truncated.iloc[i], rtol=1e-6)


def test_bollinger_no_lookahead():
    df = _sample_df()
    i = 150
    full = bollinger_width(df["close"], 20, 2.0)
    truncated = bollinger_width(df["close"].iloc[:i + 1], 20, 2.0)
    assert np.isclose(full["bb_width"].iloc[i], truncated["bb_width"].iloc[i], rtol=1e-6)


def test_build_all_indicators_no_lookahead():
    """Composite test across the full indicator pipeline at once."""
    df = _sample_df(n=400)
    i = 250
    full = build_all_indicators(df, CFG)
    truncated = build_all_indicators(df.iloc[:i + 1].copy(), CFG)
    check_cols = ["ema_9", "ema_21", "rsi", "macd_line", "atr", "bb_width"]
    for col in check_cols:
        assert np.isclose(full[col].iloc[i], truncated[col].iloc[i], rtol=1e-6, equal_nan=True), f"Leak in {col}"


def test_candle_anatomy_no_lookahead():
    """Row-wise features must be identical regardless of dataset length."""
    df = _sample_df()
    i = 150
    full = candle_anatomy(df)
    truncated = candle_anatomy(df.iloc[:i + 1].copy())
    assert np.isclose(full["body_ratio"].iloc[i], truncated["body_ratio"].iloc[i])
    assert full["candle_direction"].iloc[i] == truncated["candle_direction"].iloc[i]


def test_structure_swing_detection_no_lookahead():
    """
    A swing flag confirmed at row i using the full dataset must ALSO be confirmed
    at row i when only rows [0..i] are available - because by construction the
    confirmation only ever looks lookback candles into the past relative to i.
    """
    df = _sample_df(n=300)
    lookback = 20
    i = 200
    full = detect_structure(df, lookback=lookback)
    truncated = detect_structure(df.iloc[:i + 1].copy(), lookback=lookback)
    assert full["swing_high_confirmed"].iloc[i] == truncated["swing_high_confirmed"].iloc[i]
    assert full["swing_low_confirmed"].iloc[i] == truncated["swing_low_confirmed"].iloc[i]
    assert full["last_structure_high"].iloc[i] == truncated["last_structure_high"].iloc[i] or (
        pd.isna(full["last_structure_high"].iloc[i]) and pd.isna(truncated["last_structure_high"].iloc[i])
    )


def test_mtf_merge_asof_never_uses_future_htf_candle():
    """
    Explicit test that merge_asof backward direction never pulls an HTF candle
    whose timestamp is AFTER the LTF row's timestamp - the classic MTF leak.
    """
    ltf_df = _sample_df(n=100)  # M5
    htf_df = fetch_mock_candles("H1", n_candles=20, sessions_config=SESSIONS,
                                 end_ts=int(ltf_df["timestamp_utc"].max()))
    htf_df["dummy_feature"] = htf_df["close"]

    merged = merge_htf_feature(ltf_df, htf_df, "dummy_feature", "htf_dummy")

    htf_sorted = htf_df.sort_values("timestamp_utc").reset_index(drop=True)
    for idx, row in merged.iterrows():
        if pd.isna(row["htf_dummy"]):
            continue
        matched_htf_ts = htf_sorted.loc[htf_sorted["close"] == row["htf_dummy"], "timestamp_utc"]
        # The matched HTF candle's timestamp must always be <= the LTF row's timestamp
        valid_ts = htf_sorted[htf_sorted["timestamp_utc"] <= row["timestamp_utc"]]
        if len(valid_ts) > 0:
            expected_ts = valid_ts["timestamp_utc"].max()
            actual_htf_row = htf_sorted[htf_sorted["close"] == row["htf_dummy"]]
            if len(actual_htf_row) > 0:
                assert actual_htf_row["timestamp_utc"].iloc[0] <= row["timestamp_utc"], \
                    "Look-ahead detected: merge_asof pulled a future HTF candle!"
