"""
Tests for order flow and microstructure features.
Verifies no-lookahead invariants and metric properties.
"""
import numpy as np
import pandas as pd
import pytest

from features.order_flow import (
    cumulative_volume_delta,
    order_flow_imbalance,
    volume_weighted_average_price,
    add_order_flow_features,
)


@pytest.fixture
def sample_ohlcv():
    np.random.seed(42)
    n = 200
    close = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
    high = close + np.abs(np.random.randn(n) * 0.3)
    low = close - np.abs(np.random.randn(n) * 0.3)
    open_p = (high + low) / 2.0
    volume = np.random.randint(10, 500, size=n).astype(float)
    ts = np.arange(1000, 1000 + n * 300, 300)

    return pd.DataFrame({
        "timestamp_utc": ts,
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


def test_cvd_causality(sample_ohlcv):
    """N1: CVD is anchored to a fixed trailing window, so past the anchoring
    window its value is invariant to the total frame length (no train/serve skew).
    It is still strictly causal: it reads only bars up to and including index i."""
    full_cvd = cumulative_volume_delta(sample_ohlcv, window=100)
    # A truncated frame that still contains >= 100 bars before i must produce the
    # SAME cvd at index i as the full frame (the old window-start cumsum did not).
    i = 150
    trunc_cvd = cumulative_volume_delta(sample_ohlcv.iloc[:i + 1], window=100)
    assert np.isclose(full_cvd.iloc[i], trunc_cvd.iloc[i], rtol=1e-9)


def test_cvd_level_is_invariant_to_frame_length_beyond_window(sample_ohlcv):
    """N1 regression: the same bar must not change value when the frame is
    extended with older history (the actual train/serve skew)."""
    # Two frames sharing the SAME tail but with different amounts of leading
    # history: past the anchoring window, cvd at the shared index must match.
    # Build a longer series so the target bar is >= the anchoring window past
    # EVERY frame start (the 200-row fixture is too short for this).
    np.random.seed(1)
    n = 400
    close = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
    big = pd.DataFrame({
        "timestamp_utc": np.arange(1000, 1000 + n * 300, 300),
        "open": close,
        "high": close + 0.3,
        "low": close - 0.3,
        "close": close,
        "volume": np.random.randint(10, 500, size=n).astype(float),
    })
    base = big.iloc[150:]
    extended = big.iloc[50:]
    # Target original bar 290: base index 140 (>=100 past 150), extended index
    # 240 (>=100 past 50), full index 290. All are past the anchoring window, so
    # they must agree on the same original bar (frame-length invariant).
    base_cvd = cumulative_volume_delta(base.reset_index(drop=True), window=100)
    ext_cvd = cumulative_volume_delta(extended.reset_index(drop=True), window=100)
    full = cumulative_volume_delta(big, window=100)
    assert np.isclose(base_cvd.iloc[140], full.iloc[290], rtol=1e-9)
    assert np.isclose(ext_cvd.iloc[240], full.iloc[290], rtol=1e-9)


def test_order_flow_imbalance_bounds(sample_ohlcv):
    imbalance = order_flow_imbalance(sample_ohlcv, period=14)
    assert ((imbalance >= -1.0) & (imbalance <= 1.0)).all()
    # Causality test
    i = 80
    trunc_imb = order_flow_imbalance(sample_ohlcv.iloc[:i + 1], period=14)
    assert np.isclose(imbalance.iloc[i], trunc_imb.iloc[i], rtol=1e-6)


def test_vwap_causality(sample_ohlcv):
    vwap, upper, lower = volume_weighted_average_price(sample_ohlcv, period=50)
    assert (upper >= vwap).all()
    assert (lower <= vwap).all()
    i = 120
    t_vwap, t_upper, t_lower = volume_weighted_average_price(sample_ohlcv.iloc[:i + 1], period=50)
    assert np.isclose(vwap.iloc[i], t_vwap.iloc[i], rtol=1e-6)
    assert np.isclose(upper.iloc[i], t_upper.iloc[i], rtol=1e-6)


def test_add_order_flow_features(sample_ohlcv):
    sample_ohlcv["atr"] = 0.5
    featured = add_order_flow_features(sample_ohlcv)
    assert "cvd" in featured.columns
    assert "order_flow_imbalance_14" in featured.columns
    assert "vwap" in featured.columns
    assert "dist_vwap_atr" in featured.columns
    assert not featured["dist_vwap_atr"].isna().any()
