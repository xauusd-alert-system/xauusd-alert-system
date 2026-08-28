"""Tests for features/fractional_diff.py (Задача 3.1).

Covers:
- get_weights_ffd: identity (d=0), first-difference (d=1), weight convergence;
- frac_diff: linear -> ~0, random-walk stationarity at d=0.5 (ADF), determinism;
- frac_diff_fdf: fixed-window correctness, warm-up NaN, weight trimming;
- edge cases: NaN, inf, empty, d<0, d>1, non-finite d;
- integration: enabled=false -> build_full_df byte-identical to baseline.
"""

import numpy as np
import pandas as pd
import pytest

from features.fractional_diff import (
    frac_diff,
    frac_diff_fdf,
    get_weights_ffd,
)


def _random_walk(n: int = 600, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(np.cumsum(rng.normal(0.0, 1.0, n)), name="rw")


# ---------------------------------------------------------------------------
# get_weights_ffd
# ---------------------------------------------------------------------------


def test_get_weights_ffd_d0_identity():
    w = get_weights_ffd(0.0)
    assert np.allclose(w, [1.0])


def test_get_weights_ffd_d1_first_difference():
    w = get_weights_ffd(1.0)
    # (1 - L)^1 = [1, -1] (oldest first: -1 then 1)
    assert np.allclose(w, [-1.0, 1.0])


def test_get_weights_ffd_sum():
    """The UNtruncated weight series of (1-L)^d sums to 0 for d>0 (value of
    (1-1)^d). For d=1 the truncated result is exact ([-1, 1])."""
    assert get_weights_ffd(1.0).sum() == 0.0


def test_get_weights_ffd_decay():
    """FFD weights decay in magnitude toward zero (long memory dies out)."""
    w = np.abs(get_weights_ffd(0.4, thres=1e-6))[::-1]  # newest first
    assert w[0] == 1.0  # w_0
    assert np.all(np.diff(w) <= 1e-12)  # non-increasing
    assert w[-1] < 1e-5 * 1.05  # tail below the truncation band


def test_get_weights_ffd_monotone_truncation():
    """A smaller thres yields at least as many weights (more memory kept)."""
    assert len(get_weights_ffd(0.4, thres=1e-8)) >= len(get_weights_ffd(0.4, thres=1e-3))


def test_get_weights_ffd_rejects_d_out_of_range():
    with pytest.raises(ValueError):
        get_weights_ffd(-0.1)
    with pytest.raises(ValueError):
        get_weights_ffd(1.5)
    with pytest.raises(ValueError):
        get_weights_ffd(float("nan"))


# ---------------------------------------------------------------------------
# frac_diff
# ---------------------------------------------------------------------------


def test_frac_diff_d0_identity():
    s = _random_walk(100, seed=1)
    fd = frac_diff(s, d=0.0)
    assert np.allclose(fd.to_numpy()[10:], s.to_numpy()[10:], atol=1e-9)
    assert not fd.isna().any()


def test_frac_diff_d1_is_first_difference():
    s = _random_walk(100, seed=2)
    fd = frac_diff(s, d=1.0)
    expected = s.diff()
    assert np.allclose(fd.to_numpy()[1:], expected.to_numpy()[1:], atol=1e-9)


def test_frac_diff_linear_d1_exact_slope():
    """d=1 on a linear ramp returns exactly the constant slope."""
    s = pd.Series(np.linspace(0.0, 100.0, 300), name="lin")
    fd = frac_diff(s, d=1.0).dropna()
    assert np.allclose(fd.to_numpy(), 100.0 / 299.0, atol=1e-9)


def test_frac_diff_constant_series_d1_is_zero():
    """A constant series has no variation: d=1 output is exactly 0."""
    s = pd.Series(np.full(100, 42.0), name="const")
    fd = frac_diff(s, d=1.0).dropna()
    assert len(fd) == 99
    assert np.allclose(fd.to_numpy(), 0.0, atol=1e-12)


def test_frac_diff_stationarity_random_walk_d05():
    """Random walk at d=0.5 must pass ADF at 5% (memory preserved, trend killed).

    At thres=1e-5 the d=0.5 FFD weight window is ~2150 bars, so the series
    must be long enough to produce post-warm-up values.
    """
    from statsmodels.tsa.stattools import adfuller

    s = _random_walk(3000, seed=7)
    fd = frac_diff(s, d=0.5).dropna()
    assert len(fd) > 100
    stat, p, *_ = adfuller(fd, autolag="AIC")
    assert p < 0.05


def test_frac_diff_determinism():
    s = _random_walk(300, seed=11)
    a = frac_diff(s, d=0.4)
    b = frac_diff(s, d=0.4)
    assert a.equals(b)


def test_frac_diff_warmup_nan():
    # thres=1e-1 keeps the truncated weight window short (~11), so 60 bars
    # produce 50 valid rows after the warm-up.
    s = _random_walk(60, seed=3)
    fd = frac_diff(s, d=0.4, thresh=1e-1)
    assert fd.iloc[:10].isna().any()
    assert fd.iloc[-10:].notna().all()


def test_frac_diff_no_lookahead_truncation_invariant():
    s = _random_walk(300, seed=5)
    i = 200
    full = frac_diff(s, d=0.5)
    trunc = frac_diff(s.iloc[: i + 1], d=0.5)
    assert np.isclose(full.iloc[i], trunc.iloc[i], rtol=1e-9, equal_nan=True)


def test_frac_diff_empty_series():
    s = pd.Series([], dtype=float, name="empty")
    fd = frac_diff(s, d=0.4)
    assert fd.empty


def test_frac_diff_shorter_than_weights_all_nan():
    s = _random_walk(3, seed=4)
    fd = frac_diff(s, d=0.4)
    assert len(fd) == 3
    assert fd.isna().all()


def test_frac_diff_rejects_d_out_of_range():
    s = _random_walk(50, seed=6)
    with pytest.raises(ValueError):
        frac_diff(s, d=-0.2)
    with pytest.raises(ValueError):
        frac_diff(s, d=1.2)


def test_frac_diff_nan_propagates_not_silently_zeroed():
    """NaN inside the window -> NaN output (never a silent zero)."""
    s = _random_walk(100, seed=8)
    s.iloc[50] = np.nan
    fd = frac_diff(s, d=0.4)
    assert np.isnan(fd.iloc[60])  # window [41..60] touches the NaN at 50


def test_frac_diff_inf_propagates():
    s = _random_walk(100, seed=9)
    s.iloc[50] = np.inf
    fd = frac_diff(s, d=0.4)
    assert not np.isfinite(fd.iloc[60])


# ---------------------------------------------------------------------------
# frac_diff_fdf (fixed-width window)
# ---------------------------------------------------------------------------


def test_frac_diff_fdf_d1_matches_diff():
    s = _random_walk(100, seed=12)
    fdf = frac_diff_fdf(s, d=1.0, window=2)
    expected = s.diff()
    assert np.allclose(fdf.to_numpy()[2:], expected.to_numpy()[2:], atol=1e-9)


def test_frac_diff_fdf_d0_identity():
    s = _random_walk(100, seed=13)
    fdf = frac_diff_fdf(s, d=0.0, window=5)
    assert np.allclose(fdf.to_numpy()[4:], s.to_numpy()[4:], atol=1e-9)


def test_frac_diff_fdf_warmup_nan_and_bounded_lookback():
    s = _random_walk(200, seed=14)
    window = 40
    fdf = frac_diff_fdf(s, d=0.4, window=window)
    assert fdf.iloc[: window - 1].isna().all()
    assert fdf.iloc[window - 1 :].notna().all()
    # Bounded lookback: fdf at row i must not depend on rows before i-window+1.
    j = 150
    trunc = frac_diff_fdf(s.iloc[: j + 1], d=0.4, window=window)
    assert np.isclose(fdf.iloc[j], trunc.iloc[j], rtol=1e-9)


def test_frac_diff_fdf_matches_frac_diff_when_window_covers_weights():
    """For d=1 the FFD weight length is exactly 2 ([-1, 1]); any window >= 2
    zero-pads to it, so FDF and FFD must coincide exactly on the common support."""
    s = _random_walk(400, seed=15)
    a = frac_diff(s, d=1.0)
    b = frac_diff_fdf(s, d=1.0, window=25)
    common = a.notna() & b.notna()
    assert common.sum() > 50
    diff = (a[common] - b[common]).abs().max()
    assert diff < 1e-9


def test_frac_diff_fdf_rejects_bad_window():
    s = _random_walk(50, seed=16)
    with pytest.raises(ValueError):
        frac_diff_fdf(s, d=0.4, window=0)
    with pytest.raises(ValueError):
        frac_diff_fdf(s, d=0.4, window=-3)


def test_frac_diff_fdf_empty_series():
    s = pd.Series([], dtype=float, name="empty")
    fdf = frac_diff_fdf(s, d=0.4, window=10)
    assert fdf.empty


# ---------------------------------------------------------------------------
# Integration: build_full_df config gate
# ---------------------------------------------------------------------------


def _mini_raw(n: int = 400, seed: int = 21) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 4000.0 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame(
        {
            "timestamp_utc": 1_700_000_000 + np.arange(n) * 900,
            "open": base + rng.normal(0, 0.1, n),
            "high": base + 1.0,
            "low": base - 1.0,
            "close": base,
            "volume": rng.uniform(10, 100, n),
            "session": "london",
        }
    )


def _mini_cfg() -> dict:
    return {
        "regime": {
            "min_candles_for_regime": 50,
            "no_trade_volatility_floor": 0.0001,
            "atr_spike_multiplier": 1.8,
            "bb_width_compression_pctile": 20,
            "adx_trend_threshold": 20,
        },
        "features": {
            "ema_periods": [9, 21, 50, 200],
            "rsi_period": 14,
            "macd": {"fast": 12, "slow": 26, "signal": 9},
            "atr_period": 14,
            "bollinger": {"period": 20, "std_dev": 2.0},
            "structure_lookback": 20,
            "obv_window": 100,
        },
        "labeling": {
            "event": "barrier",
            "method": "atr_scaled",
            "horizon_candles_n": 12,
            "target_atr_multiplier": 1.2,
            "stop_atr_multiplier": 1.0,
            "atr_column": "atr",
        },
    }


def test_build_full_df_off_identical_to_baseline():
    """enabled=false/absent -> columns and values byte-identical to the pre-change pipeline."""
    from scripts.train_mt5 import build_full_df

    raw = _mini_raw()
    cfg = _mini_cfg()
    # No fractional_diff key at all (the pre-3.1 config shape).
    df_base = build_full_df(raw.copy(), cfg)
    cfg_off = {**cfg, "features": {**cfg["features"], "fractional_diff": {"enabled": False}}}
    df_off = build_full_df(raw.copy(), cfg_off)

    assert "close_fd" not in df_base.columns
    assert "close_fd" not in df_off.columns
    pd.testing.assert_frame_equal(df_base, df_off)


def test_build_full_df_on_adds_close_fd():
    from scripts.train_mt5 import build_full_df

    raw = _mini_raw()
    cfg = _mini_cfg()
    cfg_fd = {
        **cfg,
        "features": {**cfg["features"], "fractional_diff": {"enabled": True, "d": 0.4, "thres": 1e-5}},
    }
    df_base = build_full_df(raw.copy(), cfg)
    df_fd = build_full_df(raw.copy(), cfg_fd)

    assert "close_fd" in df_fd.columns
    # Only the fd column differs; the baseline columns keep their values.
    assert len(df_fd.columns) == len(df_base.columns) + 1
    shared = [c for c in df_base.columns]
    pd.testing.assert_frame_equal(df_base[shared], df_fd[shared])
    expected = frac_diff(df_fd["close"], d=0.4, thresh=1e-5)
    pd.testing.assert_series_equal(df_fd["close_fd"], expected, check_names=False)
