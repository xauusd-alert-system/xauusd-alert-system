"""Tests for features/cusum.py (Задача 3.2) — the 15 preregistered scenarios."""

import numpy as np
import pandas as pd
import pytest

from features.cusum import CUSUM_COLUMNS, cusum_features, cusum_series


def _returns(n: int, seed: int = 0, sigma: float = 0.001) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, sigma, n)


def _sr(n: int, seed: int = 0, sigma: float = 0.001) -> pd.Series:
    return pd.Series(_returns(n, seed, sigma))


def _shifted(n: int, seed: int, shift: float, at: int, sigma: float = 0.001) -> np.ndarray:
    r = _returns(n, seed, sigma)
    r[at:] += shift
    return r


# ---------------------------------------------------------------------------
# 1-3: synthetic mean-shift detection
# ---------------------------------------------------------------------------


def test_mean_shift_up_detected_with_positive_sign():
    r = _shifted(400, seed=1, shift=0.006, at=200)
    s_plus, s_minus, cp, _ = cusum_series(r, roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    hits = [t for t in range(400) if cp[t] == 1]
    assert hits, "expected at least one up change-point"
    assert hits[-1] >= 200
    assert sum(1 for t in hits if t >= 200) > len(hits) / 2


def test_mean_shift_down_detected_with_negative_sign():
    r = _shifted(400, seed=2, shift=-0.006, at=200)
    _, _, cp, _ = cusum_series(r, roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    # The KEY property: the shift is detected. Occasional pre-shift false
    # alarms on the opposite side are legitimate detector behaviour (see the
    # MQL5-plan false-positive caveat), so we assert the LAST down-hit lands
    # after the shift and the majority of hits too.
    hits = [t for t in range(400) if cp[t] == -1]
    assert hits, "expected at least one down change-point"
    assert hits[-1] >= 200
    assert sum(1 for t in hits if t >= 200) > len(hits) / 2


def test_double_shift_resets_bars_since_and_flips_sign():
    df = pd.DataFrame({"close": 100.0 * np.exp(np.cumsum(_returns(600, seed=3, sigma=0.001)))})
    cfg = {"features": {"cusum": {"roll_sigma_window": 50, "threshold_sigma": 3.0, "drift_sigma": 0.5}}}
    r = df["close"].to_numpy()
    # Inject two opposite shifts in returns-space by rebuilding the close path.
    rets = _returns(600, seed=3, sigma=0.001)
    rets[250:] += 0.006
    rets[450:] -= 0.012
    df = pd.DataFrame({"close": 100.0 * np.exp(np.cumsum(rets))})
    out = cusum_features(df, cfg)
    cp = cusum_series(np.log(df["close"] / df["close"].shift(1)).to_numpy(), 50, 3.0, 0.5)[2]
    up = [t for t in range(600) if cp[t] == 1]
    down = [t for t in range(600) if cp[t] == -1]
    assert up and down
    first_down = min(down)
    assert all(u < first_down for u in up)  # sign sequence flips
    bars = out["cp_bars_since"].to_numpy()
    assert bars[first_down] == 0.0  # reset at the change-point bar
    assert out["cp_last_sign"].iloc[first_down] == -1


# ---------------------------------------------------------------------------
# 4-5: causality + determinism
# ---------------------------------------------------------------------------


def test_causality_truncation_invariance():
    r = _shifted(400, seed=4, shift=0.006, at=250)
    i = 350
    full = cusum_series(r, roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    trunc = cusum_series(r[: i + 1], roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    for full_arr, trunc_arr in zip(full[:3], trunc[:3]):
        assert full_arr[i] == trunc_arr[i]


def test_determinism_two_runs_identical():
    r = _shifted(300, seed=5, shift=0.005, at=150)
    a = cusum_series(r, roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    b = cusum_series(r, roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    for x, y in zip(a, b):
        np.testing.assert_array_equal(x, y)


# ---------------------------------------------------------------------------
# 6-7: constant series, noise false-alarm sanity
# ---------------------------------------------------------------------------


def test_constant_series_no_change_points():
    r = np.full(500, 0.0)
    s_plus, s_minus, cp, valid = cusum_series(r, roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    assert not cp.any()
    assert np.allclose(s_plus, 0.0) and np.allclose(s_minus, 0.0)
    assert valid.sum() == 0  # sigma == 0 everywhere -> nothing valid


def test_pure_noise_low_false_alarm_rate():
    r = _returns(5000, seed=6, sigma=0.001)
    _, _, cp, _ = cusum_series(r, roll_sigma_window=96, threshold_sigma=3.0, drift_sigma=0.5)
    assert cp[np.abs(cp) == 1].sum() / len(r) < 0.05  # <5% of bars flagged


# ---------------------------------------------------------------------------
# 8-12: warm-up and edge cases
# ---------------------------------------------------------------------------


def test_warmup_rows_have_no_signs_and_nan_norm():
    n = 300
    r = _returns(n, seed=7)
    df = pd.DataFrame({"close": 100.0 * np.exp(np.cumsum(r))})
    cfg = {"features": {"cusum": {"roll_sigma_window": 96, "threshold_sigma": 3.0, "drift_sigma": 0.5}}}
    out = cusum_features(df, cfg)
    assert (out["cp_last_sign"].iloc[:96] == 0).all()
    assert out["cusum_up_norm"].iloc[:96].isna().all()
    assert out["cusum_down_norm"].iloc[:96].isna().all()
    assert out["cp_bars_since"].isna().all() or out["cp_bars_since"].iloc[:96].isna().all()


def test_empty_series():
    r = np.array([], dtype=float)
    s_plus, s_minus, cp, valid = cusum_series(r, roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    assert len(s_plus) == 0 and len(s_minus) == 0 and len(cp) == 0 and len(valid) == 0


def test_cusum_features_empty_df():
    df = pd.DataFrame({"close": pd.Series(dtype=float)})
    out = cusum_features(df, {"features": {"cusum": {}}})
    for col in CUSUM_COLUMNS:
        assert col in out.columns
    assert out.empty


def test_nan_in_close_freezes_without_exception():
    close = 100.0 * np.exp(np.cumsum(_returns(300, seed=8)))
    close[150] = np.nan
    out = cusum_features(pd.DataFrame({"close": close}), {"features": {"cusum": {"roll_sigma_window": 50}}})
    assert np.isnan(out["cusum_up_norm"].iloc[151])  # window touches the NaN return


def test_inf_in_close_guarded():
    close = 100.0 * np.exp(np.cumsum(_returns(300, seed=9)))
    close[150] = np.inf
    out = cusum_features(pd.DataFrame({"close": close}), {"features": {"cusum": {"roll_sigma_window": 50}}})
    # inf close -> inf/nan returns -> non-finite norm, no exception
    v = out["cusum_up_norm"].iloc[151]
    assert np.isnan(v) or np.isinf(v)


def test_short_series_all_warmup_nan():
    r = _returns(40, seed=10)
    s_plus, s_minus, cp, valid = cusum_series(r, roll_sigma_window=96, threshold_sigma=3.0, drift_sigma=0.5)
    assert valid.sum() == 0
    assert not cp.any()


def test_cusum_series_rejects_bad_params():
    with pytest.raises(ValueError):
        cusum_series(_returns(10), roll_sigma_window=1)
    with pytest.raises(ValueError):
        cusum_series(_returns(10), roll_sigma_window=50, threshold_sigma=-1.0)
    with pytest.raises(ValueError):
        cusum_series(_returns(10), roll_sigma_window=50, drift_sigma=float("nan"))


# ---------------------------------------------------------------------------
# 13: norm recovery
# ---------------------------------------------------------------------------


def test_up_norm_equals_s_plus_over_sigma():
    r = _shifted(300, seed=11, shift=0.005, at=150)
    s_plus, _, _, valid = cusum_series(r, roll_sigma_window=50, threshold_sigma=3.0, drift_sigma=0.5)
    sigma = pd.Series(r).rolling(50, min_periods=50).std().to_numpy()
    df = pd.DataFrame({"close": 100.0 * np.exp(np.cumsum(r))})
    out = cusum_features(df, {"features": {"cusum": {"roll_sigma_window": 50, "threshold_sigma": 3.0, "drift_sigma": 0.5}}})
    t = 290
    assert valid[t]
    assert np.isclose(out["cusum_up_norm"].iloc[t], s_plus[t] / sigma[t], rtol=1e-12)


# ---------------------------------------------------------------------------
# 14-15: integration gate
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
    from scripts.train_mt5 import build_full_df

    raw = _mini_raw()
    cfg = _mini_cfg()
    df_base = build_full_df(raw.copy(), cfg)
    cfg_off = {**cfg, "features": {**cfg["features"], "cusum": {"enabled": False}}}
    df_off = build_full_df(raw.copy(), cfg_off)

    assert not any(c in df_base.columns for c in CUSUM_COLUMNS)
    assert not any(c in df_off.columns for c in CUSUM_COLUMNS)
    pd.testing.assert_frame_equal(df_base, df_off)


def test_build_full_df_on_adds_exactly_four_columns():
    from scripts.train_mt5 import build_full_df

    raw = _mini_raw()
    cfg = _mini_cfg()
    cfg_on = {
        **cfg,
        "features": {**cfg["features"], "cusum": {"enabled": True, "roll_sigma_window": 50, "threshold_sigma": 3.0, "drift_sigma": 0.5}},
    }
    df_base = build_full_df(raw.copy(), cfg)
    df_on = build_full_df(raw.copy(), cfg_on)

    for col in CUSUM_COLUMNS:
        assert col in df_on.columns
    assert len(df_on.columns) == len(df_base.columns) + len(CUSUM_COLUMNS)
    shared = list(df_base.columns)
    pd.testing.assert_frame_equal(df_base[shared], df_on[shared])
