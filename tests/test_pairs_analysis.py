# -*- coding: utf-8 -*-
"""Unit tests for the pairs-analysis core (ТЗ §7.1).

Synthetic series with known properties:
  - cointegrated pair  -> ADF p < 0.05, half-life in the expected range
  - random-walk spread -> ADF p high (no cointegration -> no edge)
  - OU with known θ    -> half-life ≈ ln(2)/θ within ±20%
  - z-score vs pandas reference to 1e-6
  - Kalman β tracks a step change and matches OLS on a static β
Plus data-layer tests (align, resample, CSV, Binance cache round-trip)
and an end-to-end analyzer run on the real MT5 sqlite (XAU/XAG, D1).
"""
import datetime as dt
import os
import sqlite3
import tempfile

import numpy as np
import pandas as pd
import pytest

from pairs_analysis import metrics, data, PairAnalyzer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MT5_DB = os.path.join(ROOT, "data", "market_data_mt5.sqlite")


# ---------------------------------------------------------------------------
# synthetic series
# ---------------------------------------------------------------------------

def _synth_cointegrated(n=600, beta_true=1.5, alpha=0.5, rho=0.85,
                        sigma_eps=0.2, seed=1):
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.normal(0, 1.0, n))
    eta = np.empty(n)
    eta[0] = 0.0
    for t in range(1, n):
        eta[t] = rho * eta[t - 1] + rng.normal(0, sigma_eps)
    y = alpha + beta_true * x + eta
    return x, y


def _synth_ou(theta=0.1, n=2000, sigma=0.1, seed=5):
    rng = np.random.default_rng(seed)
    e = np.empty(n)
    e[0] = 0.0
    rho = 1.0 - theta
    for t in range(1, n):
        e[t] = rho * e[t - 1] + rng.normal(0, sigma)
    return e


def _as_series(x, y):
    idx = pd.date_range("2024-01-01", periods=len(x), freq="D", tz="UTC")
    return pd.Series(x, index=idx), pd.Series(y, index=idx)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

class TestZScore:
    def test_matches_pandas_reference(self):
        x, y = _synth_cointegrated(seed=11)
        xs, ys = _as_series(x, y)
        e = metrics.spread(ys, xs, 1.5)
        z = metrics.zscore(e, 90)
        mu = e.rolling(90).mean()
        sd = e.rolling(90).std(ddof=1)
        ref = (e - mu) / sd
        np.testing.assert_allclose(z.dropna().to_numpy(),
                                   ref.dropna().to_numpy(),
                                   rtol=1e-6, atol=1e-6)


class TestCointegration:
    def test_cointegrated_pair_is_stationary(self):
        x, y = _synth_cointegrated(seed=2)
        xs, ys = _as_series(x, y)
        e = metrics.spread(ys, xs, 1.5)
        p = metrics.adf_pvalue(e)
        assert p < 0.05

    def test_random_walk_spread_not_cointegrated(self):
        rng = np.random.default_rng(7)
        x = np.cumsum(rng.normal(0, 1.0, 600))
        y = np.cumsum(rng.normal(0, 1.0, 600))   # independent walk
        xs, ys = _as_series(x, y)
        e = metrics.spread(ys, xs, 1.0)
        p = metrics.adf_pvalue(e)
        assert p > 0.05

    def test_half_life_matches_ou(self):
        e = _synth_ou(theta=0.1, seed=5)
        es = pd.Series(e, index=pd.date_range("2024-01-01", periods=len(e), freq="D", tz="UTC"))
        theta, hl_bars = metrics.half_life(es)
        assert np.isfinite(hl_bars)
        expected = np.log(2.0) / 0.1
        assert abs(hl_bars - expected) / expected <= 0.20
        assert abs(theta - 0.1) / 0.1 <= 0.20


class TestBeta:
    def test_kalman_tracks_step_change(self):
        rng = np.random.default_rng(3)
        n = 600
        x = np.cumsum(rng.normal(0, 1.0, n))
        b_true = np.concatenate([np.full(n // 2, 1.0), np.full(n - n // 2, 2.0)])
        y = b_true * x + rng.normal(0, 0.1, n)
        xs, ys = _as_series(x, y)
        kb = metrics.kalman_beta(xs, ys, q=1e-3, r=1e-2)
        assert abs(kb[-1] - 2.0) < 0.3

    def test_kalman_matches_ols_on_static_beta(self):
        rng = np.random.default_rng(4)
        x = np.cumsum(rng.normal(0, 1.0, 500))
        y = 0.5 + 1.5 * x + rng.normal(0, 0.5, 500)
        xs, ys = _as_series(x, y)
        kb = metrics.kalman_beta(xs, ys)
        ols_full = float(np.polyfit(x, y, 1)[0])
        assert abs(kb[-1] - ols_full) < 0.2
        assert abs(kb[-1] - 1.5) < 0.2


class TestMathBoard:
    def test_hurst_trending_vs_meanreverting(self):
        """R/S по доходностям (приращениям): H<0.5 mean-reverting, H>0.5
        персистентный/трендовый, H≈0.5 случайное блуждание (ТЗ §4.2)."""
        rng = np.random.default_rng(9)
        n = 1200
        idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        # моментум: доходности AR(1) с положительной автокорреляцией
        mom = np.empty(n)
        mom[0] = 0.0
        for t in range(1, n):
            mom[t] = 0.3 * mom[t - 1] + rng.normal(0, 1)
        # mean-reverting: приращения OU-спреда (анти-персистентные).
        # ρ=0.6 (быстрая реверсия) даёт H≈0.40 — стабильно ниже 0.5.
        ou_lv = np.empty(n)
        ou_lv[0] = 0.0
        for t in range(1, n):
            ou_lv[t] = 0.6 * ou_lv[t - 1] + rng.normal(0, 0.2)
        trend = pd.Series(mom, index=idx)
        meanrev = pd.Series(np.diff(ou_lv), index=idx[1:])
        assert metrics.hurst_rs(trend) > 0.5
        assert metrics.hurst_rs(meanrev) < 0.5


# ---------------------------------------------------------------------------
# data layer
# ---------------------------------------------------------------------------

class TestData:
    def test_align_drops_missing_bars(self):
        idx = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
        a = pd.DataFrame({"close": np.arange(10.0)}, index=idx)
        idx2 = idx[[0, 1, 2, 5, 6, 7, 8, 9]]     # gap at 3-4
        b = pd.DataFrame({"close": np.arange(8.0)}, index=idx2)
        a2, b2 = data.align(a, b)
        assert len(a2) == len(b2) == 8
        assert (a2.index == b2.index).all()

    def test_resample_ohlcv(self):
        idx = pd.date_range("2024-01-01 00:00", periods=6, freq="15min", tz="UTC")
        df = pd.DataFrame({
            "open": [1, 2, 3, 4, 5, 6],
            "high": [2, 3, 4, 5, 6, 7],
            "low": [0.5, 1.5, 2.5, 3.5, 4.5, 5.5],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
            "volume": [10, 20, 30, 40, 50, 60],
        }, index=idx)
        h = data.resample(df, "H1")
        assert len(h) == 2
        assert h.iloc[0]["open"] == 1.0
        assert h.iloc[0]["high"] == 5.0       # бары 00:00..00:45 -> max 5
        assert h.iloc[0]["low"] == 0.5
        assert h.iloc[0]["close"] == 4.5
        assert h.iloc[0]["volume"] == 100.0

    def test_csv_loader_flexible_columns(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "data.csv")
            with open(p, "w", encoding="utf-8") as f:
                f.write("Date,Open,High,Low,Close,Vol\n"
                        "2024-01-01,1,2,0.5,1.5,10\n"
                        "2024-01-02,1.5,2.5,1,2,20\n")
            df = data.load_csv(p)
            assert len(df) == 2
            assert list(df.columns) == ["open", "high", "low", "close", "volume"]
            assert df.index.tz is not None
            assert df["close"].iloc[-1] == 2.0

    def test_binance_cache_roundtrip(self):
        """Полностью покрытый кэшем диапазон отдаётся без сети."""
        with tempfile.TemporaryDirectory() as td:
            cache = os.path.join(td, "cache.sqlite")
            start = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
            end = dt.datetime(2024, 1, 3, tzinfo=dt.timezone.utc)
            sms = int(start.timestamp() * 1000)
            ems = int(end.timestamp() * 1000)
            data._init_cache(cache)
            con = sqlite3.connect(cache)
            try:
                for i, o in enumerate([100.0, 101.0, 102.0]):
                    t = sms + i * 86400000
                    con.execute(
                        "INSERT OR REPLACE INTO binance_klines "
                        "(symbol, interval, open_time, open, high, low, close, volume) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        ("BTCUSDT", "1d", t, o, o + 1, o - 1, o + 0.5, 1000 + i))
                con.commit()
            finally:
                con.close()
            df = data.fetch_binance("BTCUSDT", "D1", start=start, end=end, cache_path=cache)
            assert len(df) == 3
            assert df["close"].iloc[-1] == 102.5


# ---------------------------------------------------------------------------
# end-to-end on real data
# ---------------------------------------------------------------------------

class TestAnalyzer:
    def test_analyzer_mt5_xau_xag_d1(self):
        if not os.path.exists(MT5_DB):
            pytest.skip("MT5-база отсутствует")
        pair = {"name": "XAU/XAG", "source": "mt5", "symbols": ["XAUUSD", "XAGUSD"]}
        cfg = {"window": 90, "ols_window": 90, "kalman_q": 1e-4, "kalman_r": 1e-2,
               "default_timeframe": "D1"}
        m = PairAnalyzer(pair, cfg).analyze()
        assert m.n_bars >= 100
        assert np.isfinite(m.beta)
        assert m.ratio > 0
        assert np.isfinite(m.adf_p)
        assert len(m.spread) == len(m.zscore) == m.n_bars
        assert "e_t =" in m.formula_str
        s = m.summary()
        assert s["name"] == "XAU/XAG"
        assert s["beta_method"] in ("kalman", "ols")

    def test_analyzer_mt5_eur_gbp_h1(self):
        if not os.path.exists(MT5_DB):
            pytest.skip("MT5-база отсутствует")
        pair = {"name": "EURUSD/GBPUSD", "source": "mt5", "symbols": ["EURUSD", "GBPUSD"]}
        cfg = {"window": 90, "default_timeframe": "H1"}
        m = PairAnalyzer(pair, cfg).analyze()
        assert m.n_bars >= 100
        assert np.isfinite(m.beta)
