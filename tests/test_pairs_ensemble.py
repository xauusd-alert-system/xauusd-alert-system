# -*- coding: utf-8 -*-
"""Tests for ensemble forecast engines (ТЗ §4.3, этап 3)."""
import numpy as np
import pandas as pd
import pytest

from pairs_analysis import PairAnalyzer, EnsembleEngine
from pairs_analysis.ensemble import (
    engine_ou, engine_kalman_trend, engine_garch,
    engine_gbm_mc, engine_heston, engine_bayesian_regime,
    ENGINE_NAMES,
)
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MT5_DB = os.path.join(ROOT, "data", "market_data_mt5.sqlite")


def _fake_metrics(spread_vals, p1_close=None, n=300):
    """Build a minimal PairMetrics for testing engines without data loading."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    e = pd.Series(spread_vals[:n], index=idx)
    z = (e - e.rolling(90).mean()) / e.rolling(90).std(ddof=1)
    if p1_close is not None:
        p1 = pd.DataFrame({"close": p1_close[:n]}, index=idx)
    else:
        p1 = pd.DataFrame({"close": np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, n)))}, index=idx)

    # Theta/half-life from regression
    s = e.dropna().to_numpy()
    if len(s) > 10:
        de = np.diff(s)
        lag = s[:-1]
        A = np.column_stack([np.ones(len(lag)), lag])
        coef = np.linalg.lstsq(A, de, rcond=None)[0]
        theta = -float(coef[1])
        hl = np.log(2) / theta if theta > 0 else float("inf")
    else:
        theta, hl = 0.0, float("inf")

    return type("M", (), {
        "name": "SYN/TEST", "timeframe": "D1", "n_bars": n,
        "start": str(idx[0].date()), "end": str(idx[-1].date()),
        "spread": e, "zscore": z,
        "mu": float(e.mean()), "sigma": float(e.std(ddof=1)),
        "sigma_annual": float(e.std(ddof=1) * np.sqrt(252)),
        "theta": theta, "half_life_bars": hl, "half_life_days": hl,
        "adf_p": 0.01, "hurst": 0.45,
        "skew": 0.0, "ex_kurtosis": 0.0, "acf1": -0.05,
        "realized_vol_pct": float(e.diff().std() * 100),
        "beta": 1.5, "beta_method": "kalman",
        "beta_series": pd.Series(np.ones(n) * 1.5, index=idx),
        "ratio": 100.0, "p1_last": 2000.0, "p2_last": 20.0,
        "formula_str": "e_t = ...",
        "p1": p1, "p2": p1,
    })()


class TestOUEngine:
    def test_short_when_z_positive(self):
        # z > 0 → spread rich → short P1
        vals = np.concatenate([np.zeros(200), np.linspace(0, 3.0, 100)])
        m = _fake_metrics(vals)
        r = engine_ou(m)
        assert r.direction == "short"

    def test_long_when_z_negative(self):
        vals = np.concatenate([np.zeros(200), np.linspace(0, -3.0, 100)])
        m = _fake_metrics(vals)
        r = engine_ou(m)
        assert r.direction == "long"

    def test_neutral_when_z_small(self):
        # Fixed z near 0 via direct metric mock
        m = _fake_metrics(np.zeros(300))
        m.zscore = pd.Series(np.zeros(300), index=m.spread.index)
        r = engine_ou(m)
        assert r.direction == "neutral"

    def test_confidence_scales_with_z(self):
        """Larger |z| → higher confidence."""
        vals_far = np.concatenate([np.zeros(200), np.linspace(0, 2.5, 100)])
        vals_near = np.concatenate([np.zeros(200), np.linspace(0, 1.2, 100)])
        m_far = _fake_metrics(vals_far)
        m_near = _fake_metrics(vals_near)
        assert engine_ou(m_far).confidence > engine_ou(m_near).confidence


class TestKalmanTrend:
    def test_returns_valid_result(self):
        vals = np.concatenate([np.zeros(200), np.linspace(0, 1.0, 100)])
        m = _fake_metrics(vals)
        r = engine_kalman_trend(m)
        assert r.name == "KalmanTrend"
        assert r.direction in ("long", "short", "neutral")
        assert 0 <= r.confidence <= 100


class TestGARCH:
    def test_returns_valid_result(self):
        rng = np.random.default_rng(42)
        p1 = np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
        vals = rng.normal(0, 0.1, 300)
        m = _fake_metrics(vals, p1_close=p1)
        r = engine_garch(m)
        assert r.name == "GARCH"
        assert r.direction == "neutral"  # GARCH doesn't predict direction
        assert 0 <= r.confidence <= 100
        # Should have GARCH parameters
        assert "alpha" in r.details
        assert "vol_ratio" in r.details

    def test_vol_ratio_affects_confidence(self):
        """Low vol_ratio → higher confidence (favorable for mean-rev)."""
        rng = np.random.default_rng(7)
        # Low vol regime
        p1_low = np.exp(np.cumsum(rng.normal(0, 0.005, 300)))
        m_low = _fake_metrics(np.zeros(300), p1_close=p1_low)
        r_low = engine_garch(m_low)
        # High vol regime (add jumps)
        p1_high = np.exp(np.cumsum(rng.normal(0, 0.05, 300)))
        m_high = _fake_metrics(np.zeros(300), p1_close=p1_high)
        r_high = engine_garch(m_high)
        # Low vol should have higher or equal confidence
        assert r_low.confidence >= r_high.confidence


class TestGBMMC:
    def test_returns_valid_result(self):
        rng = np.random.default_rng(42)
        p1 = np.exp(np.cumsum(rng.normal(0.001, 0.02, 300)))
        vals = rng.normal(0, 0.1, 300)
        m = _fake_metrics(vals, p1_close=p1)
        r = engine_gbm_mc(m)
        assert r.name == "GBM_MC"
        assert r.direction in ("long", "short", "neutral")
        assert 0 <= r.confidence <= 100
        assert "P_up" in r.details
        assert "E_S" in r.details

    def test_positive_drift_gives_long(self):
        """Strong upward drift → P(up) > 55% → long."""
        rng = np.random.default_rng(10)
        p1 = np.exp(np.cumsum(rng.normal(0.01, 0.005, 300)))
        vals = rng.normal(0, 0.1, 300)
        m = _fake_metrics(vals, p1_close=p1)
        r = engine_gbm_mc(m)
        assert r.direction == "long"
        assert r.details["P_up"] > 55


class TestHeston:
    def test_returns_valid_result(self):
        rng = np.random.default_rng(42)
        p1 = np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
        vals = rng.normal(0, 0.1, 300)
        m = _fake_metrics(vals, p1_close=p1)
        r = engine_heston(m)
        assert r.name == "Heston"
        assert r.direction == "neutral"  # Heston doesn't predict direction
        assert 0 <= r.confidence <= 100
        assert "xi_vol_of_vol" in r.details


class TestBayesianRegime:
    def test_mean_rev_regime_when_features_favor(self):
        # Create a mean-reverting spread: ADF p will be low, Hurst low
        rng = np.random.default_rng(42)
        e = np.zeros(300)
        for t in range(1, 300):
            e[t] = 0.8 * e[t - 1] + rng.normal(0, 0.05)  # mean-reverting
        m = _fake_metrics(e)
        r = engine_bayesian_regime(m)
        assert r.name == "BayesRegime"
        # Mean-reverting spread → ADF p low, Hurst low → P(mean-rev) > 50
        assert r.details["p_mean_rev"] > 50

    def test_trending_regime_when_features_oppose(self):
        # Create a trending spread: ADF p will be high, Hurst high
        rng = np.random.default_rng(42)
        e = np.cumsum(rng.normal(0.05, 0.1, 300))  # random walk with drift
        m = _fake_metrics(e)
        r = engine_bayesian_regime(m)
        # Trending spread → ADF p high, Hurst high → P(mean-rev) < 50
        assert r.details["p_mean_rev"] < 50


class TestEnsembleAggregation:
    def test_equal_weights_default(self):
        e = EnsembleEngine()
        assert len(e.weights) == 6
        assert all(w == 1.0 for w in e.weights)

    def test_custom_weights(self):
        e = EnsembleEngine({"ensemble": {"weights": [2, 1, 1, 1, 1, 0.5]}})
        assert e.weights[0] == 2.0
        assert e.weights[5] == 0.5

    def test_forecast_returns_all_engines(self):
        rng = np.random.default_rng(42)
        vals = np.concatenate([np.zeros(200), np.linspace(0, 2.0, 100)])
        p1 = np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
        m = _fake_metrics(vals, p1_close=p1)
        e = EnsembleEngine()
        f = e.forecast(m)
        assert f.pair_name == "SYN/TEST"
        assert len(f.engines) == 6
        assert f.direction in ("long", "short", "neutral")
        assert 0 <= f.confidence <= 100

    def test_summary_line_format(self):
        rng = np.random.default_rng(42)
        vals = np.concatenate([np.zeros(200), np.linspace(0, 2.0, 100)])
        p1 = np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
        m = _fake_metrics(vals, p1_close=p1)
        f = EnsembleEngine().forecast(m)
        line = f.summary_line()
        assert "ENSEMBLE →" in line
        assert "SYN" in line
        assert "CONF" in line

    def test_as_dict(self):
        rng = np.random.default_rng(42)
        vals = np.concatenate([np.zeros(200), np.linspace(0, 2.0, 100)])
        p1 = np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
        m = _fake_metrics(vals, p1_close=p1)
        f = EnsembleEngine().forecast(m)
        d = f.as_dict()
        assert "pair" in d
        assert "engines" in d
        assert len(d["engines"]) == 6


class TestRealData:
    @pytest.mark.skipif(not os.path.exists(MT5_DB), reason="MT5-база отсутствует")
    def test_xau_xag_ensemble(self):
        pair = {"name": "XAU/XAG", "source": "mt5", "symbols": ["XAUUSD", "XAGUSD"]}
        pa = PairAnalyzer(pair, {"window": 90, "kalman_q": 1e-4, "kalman_r": 0.01})
        m = pa.analyze("D1")
        e = EnsembleEngine()
        f = e.forecast(m)
        assert len(f.engines) == 6
        assert f.direction in ("long", "short", "neutral")
        print(f"\n{f.summary_line()}")
        for eng in f.engines:
            print(f"  {eng.name:15s}: {eng.direction:8s} {eng.confidence:5.1f}% "
                  f"| {eng.details}")
