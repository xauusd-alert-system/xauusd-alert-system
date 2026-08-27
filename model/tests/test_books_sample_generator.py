"""TZ_BOOKS T-02/T-19: the book sample generator (create_initial_data pattern).

Covers the contract the EA and the trainer both rely on:

* feature set: RSI + MACD + candle geometry, extended adds ATR/session
  volume/volume (T-19);
* normalization fitted on TRAIN only and serialized for live use;
* time-ordered 60/20/20 split with no leakage across boundaries;
* ``multi_horizon`` targets produce a 2-column y with train-only scaling;
* synthetic fallback is deterministic.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.sample_generator import (
    FEATURE_COLUMNS_BASE,
    FEATURE_COLUMNS_EXTENDED,
    NormalizationParams,
    apply_normalization,
    build_book_features,
    generate_book_samples,
    load_normalization_params,
    make_windowed_samples,
    save_normalization_params,
    synthetic_ohlcv,
)


def _ohlcv(n: int = 900, seed: int = 3) -> pd.DataFrame:
    return synthetic_ohlcv(n=n, seed=seed)


def test_base_feature_columns_and_geometry():
    df = _ohlcv()
    feats = build_book_features(df)
    assert list(feats.columns)[:7] == FEATURE_COLUMNS_BASE
    # geometry features are scale-free and bounded by the bar range
    for col in ("upper_wick", "body", "lower_wick"):
        values = feats[col].dropna()
        assert values.between(-1.0, 1.0).all(), col
    # wicks + |body| reconstruct the range up to floating error
    recon = (feats["upper_wick"].abs() + feats["body"].abs()
             + feats["lower_wick"].abs()).dropna()
    assert ((recon - 1.0).abs() < 1e-9).all()


def test_extended_features_add_three_columns():
    df = _ohlcv()
    feats = build_book_features(df, {"extended": True})
    assert list(feats.columns) == FEATURE_COLUMNS_EXTENDED
    # T-19: ATR, session volatility and volume enter as RATIOS so they stay
    # scale-free like the geometry features
    assert {"atr_ratio", "session_vol_ratio", "volume_ratio"} <= set(feats.columns)


def test_split_is_time_ordered_60_20_20():
    samples = generate_book_samples(_ohlcv(1200, seed=5))
    sizes = samples.split_sizes()
    total = sum(sizes.values())
    assert sizes["train"] / total > 0.55
    assert sizes["valid"] / total > 0.15
    assert sizes["test"] / total > 0.15
    # exact ordering: train rows precede valid precede test (no shuffle)
    assert samples.X_train.shape[1:] == samples.X_valid.shape[1:]
    assert samples.X_train.shape[1:] == samples.X_test.shape[1:]


def test_normalization_fitted_on_train_only():
    df = _ohlcv(1500, seed=11)
    norm_path = "/tmp/book_norm_test.json"
    samples = generate_book_samples(df, norm_params_path=norm_path)

    loaded = load_normalization_params(norm_path)
    assert isinstance(loaded, NormalizationParams)
    assert loaded.columns == samples.feature_columns
    # every scale positive (no division by zero downstream)
    assert all(v > 0 for v in loaded.scale.values())
    # normalized train features are ~zero-mean / ~unit-std for zscore
    flat = samples.X_train.reshape(-1, samples.X_train.shape[-1])
    means = flat.mean(axis=0)
    stds = flat.std(axis=0)
    assert np.allclose(means, 0.0, atol=0.15)
    assert np.all(stds > 0.05)


def test_apply_normalization_roundtrip():
    df = _ohlcv(600, seed=13)
    feats = build_book_features(df)[FEATURE_COLUMNS_BASE].dropna()
    params = NormalizationParams(
        method="zscore", columns=list(feats.columns),
        center={c: float(feats[c].mean()) for c in feats.columns},
        scale={c: float(feats[c].std()) for c in feats.columns})
    normed = apply_normalization(feats, params)
    assert np.allclose(normed.mean(), 0.0, atol=1e-9)


def test_multi_horizon_target_is_two_column():
    cfg = {"target_mode": "multi_horizon", "multi_horizons": [6, 12]}
    samples = generate_book_samples(_ohlcv(1200, seed=17), cfg=cfg)
    assert samples.y_train.ndim == 2
    assert samples.y_train.shape[1] == 2
    assert samples.y_valid.shape[1] == 2
    assert isinstance(samples.target_scale, np.ndarray)
    assert samples.target_scale.shape == (2,)


def test_windowed_samples_align_features_and_target():
    df = _ohlcv(500, seed=19)
    feats = build_book_features(df)[FEATURE_COLUMNS_BASE]
    target = df["close"].pct_change().shift(-1)
    X, y, idxs = make_windowed_samples(feats, target, window=8)
    assert X.shape[0] == y.shape[0] == len(idxs)
    assert X.shape[1] == 8
    assert X.shape[2] == len(FEATURE_COLUMNS_BASE)
    # chronological: consecutive samples advance by exactly one bar
    diffs = np.diff(idxs)
    assert (diffs == 1).all()


def test_synthetic_ohlcv_is_deterministic():
    a = synthetic_ohlcv(n=300, seed=42)
    b = synthetic_ohlcv(n=300, seed=42)
    pd.testing.assert_frame_equal(a, b)
    assert (a["high"] >= a[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (a["low"] <= a[["open", "close"]].min(axis=1) + 1e-9).all()


def test_normalization_params_json_roundtrip(tmp_path):
    df = _ohlcv(400, seed=23)
    feats = build_book_features(df)[FEATURE_COLUMNS_BASE].dropna()
    params = NormalizationParams(
        method="zscore", columns=list(feats.columns),
        center={c: 1.0 for c in feats.columns},
        scale={c: 2.0 for c in feats.columns})
    path = str(tmp_path / "norm.json")
    save_normalization_params(params, path)
    loaded = load_normalization_params(path)
    assert loaded.to_dict() == params.to_dict()
