# -*- coding: utf-8 -*-
"""Tests for SignalEngine (ТЗ §4.4, §7.2): пороги, гейты, выходы,
point-in-time (без look-ahead), walk-forward на синтетике и реальных данных."""
import datetime as dt
import os

import numpy as np
import pandas as pd
import pytest

from pairs_analysis import metrics, PairAnalyzer, SignalEngine
from pairs_analysis.signal import _simulate_position

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MT5_DB = os.path.join(ROOT, "data", "market_data_mt5.sqlite")

THRESH = {"entry_z": 2.0, "exit_z": 0.0, "stop_z": 3.0}
CFG = {"window": 90, "gate_window": 250, "min_start_bars": 250,
       "adf_p_max": 0.05, "half_life_range_days": [2.0, 60.0],
       "hurst_meanrev_max": 0.5, "kalman_q": 1e-4, "kalman_r": 1e-2}


def _coint_pair_with_shocks(n=1200, seed=13, rho=0.7, shock=2.2):
    """Пара в лог-пространстве: ln(P1) random walk, ln(P2) = 0.3 + 1.3·ln(P1) + e,
    e = OU(ρ) + инъекции ±shock, заставляющие z пересекать 2σ в известные
    моменты. Цены всегда положительны (exp), лог-спред корректен."""
    rng = np.random.default_rng(seed)
    ln_x = np.cumsum(rng.normal(0, 0.01, n))
    e = np.empty(n)
    e[0] = 0.0
    for t in range(1, n):
        e[t] = rho * e[t - 1] + rng.normal(0, 0.05)
    e[300] += shock
    e[600] -= shock
    e[900] += shock
    ln_y = 0.3 + 1.3 * ln_x + e
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return (pd.DataFrame({"close": np.exp(ln_x)}, index=idx),
            pd.DataFrame({"close": np.exp(ln_y)}, index=idx))


def _indep_walks(n=1200, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return (pd.DataFrame({"close": np.exp(np.cumsum(rng.normal(0, 0.01, n)))}, index=idx),
            pd.DataFrame({"close": np.exp(np.cumsum(rng.normal(0, 0.01, n)))}, index=idx))


def _engine(**over):
    c = dict(CFG)
    c.update(over)
    return SignalEngine(THRESH, c)


class TestExits:
    def test_exit_z(self):
        z = np.array([-2.5, -2.0, -1.0, 0.4, 1.0])
        idx, reason, zx = _simulate_position("long", 0, -2.5, z, 3.0, 0.0, 10.0, len(z) - 1)
        assert reason == "exit_z"
        assert idx == 3
        assert zx == pytest.approx(0.4)
        # R = (z1 - z0) / (stop + z0) = (0.4 + 2.5) / 0.5 = 5.8
        assert (zx - z[0]) / (3.0 + z[0]) == pytest.approx(5.8)

    def test_stop_z_r_minus_one(self):
        z = np.array([2.4, 2.6, 3.1, 1.0])
        idx, reason, zx = _simulate_position("short", 0, 2.4, z, 3.0, 0.0, 10.0, len(z) - 1)
        assert reason == "stop_z"
        assert idx == 2
        # R = (z0 - z1) / (stop - z0) = (2.4 - 3.1) / 0.6 = -1.166... (переход за стоп)
        r = (z[0] - zx) / (3.0 - z[0])
        assert r == pytest.approx(-7.0 / 6.0)

    def test_timeout_after_two_hl(self):
        # HL=5 баров -> таймаут на (2*5)=10-м баре после входа
        z = np.full(40, 2.2)
        idx, reason, _ = _simulate_position("short", 0, 2.2, z, 3.0, 0.0, 5.0, len(z) - 1)
        assert reason == "timeout"
        assert idx == 10

    def test_end_of_data(self):
        z = np.full(20, -2.1)
        idx, reason, _ = _simulate_position("long", 0, -2.1, z, 3.0, 0.0, 100.0, len(z) - 1)
        assert reason == "end_of_data"
        assert idx == len(z) - 1


class TestWalkForward:
    def test_random_walk_no_trades(self):
        p1, p2 = _indep_walks()
        res = _engine().walk_forward(p1, p2, "RW/RW", "D1")
        assert res.trades == []

    def test_cointegrated_pair_fires_and_profits(self):
        p1, p2 = _coint_pair_with_shocks()
        res = _engine().walk_forward(p1, p2, "SYN", "D1")
        assert len(res.trades) >= 2
        assert all(abs(t.entry_z) >= 2.0 for t in res.trades)
        assert all(t.exit_reason in ("exit_z", "stop_z", "timeout", "end_of_data") for t in res.trades)
        s = res.summary()
        assert s["avg_r"] > 0
        assert s["n_trades"] == len(res.trades)

    def test_no_entry_beyond_stop_z(self):
        # серия с мгновенным |z| >= stop: вход не должен произойти
        p1, p2 = _coint_pair_with_shocks(n=1200, seed=21, shock=4.0)
        res = _engine().walk_forward(p1, p2, "SYN", "D1")
        assert all(abs(t.entry_z) < 3.0 for t in res.trades)

    def test_hurst_gate_blocks_trending(self):
        # Тестируем гейт Hurst изолированно: если H >= 0.5,
        # _gates_ok возвращает False (ТЗ §4.5).
        # Независимые трендовые ряды МОГУТ выдать ADF p < 0.05 на малом окне
        # из-за адаптации Калман-беты, поэтому walk_forward ненадёжен для
        # этого теста — проверяем гейт напрямую.
        eng = _engine()
        g_trending = {"adf_p": 0.001, "half_life_bars": 10.0, "hurst": 0.65}
        ok, _ = eng._gates_ok(g_trending, "D1")
        assert not ok, "Hurst > 0.5 должен блокировать сигнал"
        g_mixed = {"adf_p": 0.001, "half_life_bars": 10.0, "hurst": 0.50}
        ok2, _ = eng._gates_ok(g_mixed, "D1")
        assert not ok2, "Hurst == 0.5 (порог строгий >=) должен блокировать"
        g_mr = {"adf_p": 0.001, "half_life_bars": 10.0, "hurst": 0.45}
        ok3, _ = eng._gates_ok(g_mr, "D1")
        assert ok3, "Hurst < 0.5 должен разрешать mean-rev"

    def test_no_lookahead(self):
        """Гейты входа в полном прогоне == гейтам в прогоне, обрезанном на
        баре входа (Калман point-in-time: β_t зависит только от данных <= t)."""
        p1, p2 = _coint_pair_with_shocks(seed=7)
        eng = _engine()
        res = eng.walk_forward(p1, p2, "SYN", "D1")
        assert res.trades
        t0 = res.trades[0]
        mask = p1.index <= pd.Timestamp(t0.entry_ts, tz="UTC")
        res_trunc = _engine().walk_forward(p1.loc[mask], p2.loc[mask], "SYN", "D1")
        assert res_trunc.trades, "обрезанный прогон должен дать ту же первую сделку"
        t0b = res_trunc.trades[0]
        assert t0b.entry_ts == t0.entry_ts
        assert t0b.adf_p == t0.adf_p
        assert t0b.half_life_bars == t0.half_life_bars
        assert t0b.hurst == t0.hurst
        assert t0b.beta == t0.beta


class TestCurrentSignal:
    def _metrics(self, z_last, adf_p=0.001, hurst=0.4, hl_days=7.0):
        n = 300
        idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        z = pd.Series(np.linspace(-1.0, z_last, n), index=idx)
        e = pd.Series(np.random.default_rng(1).normal(0, 1, n), index=idx)
        return _FakeMetrics(z, e, adf_p, hurst, hl_days)

    def test_stand_aside_when_z_small(self):
        s = _engine().current(self._metrics(1.0))
        assert s.direction == "none"
        assert "STAND ASIDE" in s.reason
        assert not s.valid

    def test_mean_rev_short_when_z_high(self):
        s = _engine().current(self._metrics(2.4))
        assert s.direction == "short"
        assert s.valid
        assert "MEAN-REV SHORT" in s.reason

    def test_mean_rev_long_when_z_low(self):
        s = _engine().current(self._metrics(-2.3))
        assert s.direction == "long"
        assert s.valid

    def test_regime_blocks(self):
        s = _engine().current(self._metrics(2.4, hurst=0.7))
        assert s.direction == "none"
        assert not s.valid


class _FakeMetrics:
    def __init__(self, zscore, spread, adf_p, hurst, hl_days):
        self.name = "FAKE"
        self.timeframe = "D1"
        self.end = "2025-12-31"
        self.zscore = zscore
        self.spread = spread
        self.adf_p = adf_p
        self.hurst = hurst
        self.half_life_bars = hl_days
        self.half_life_days = hl_days


class TestRealData:
    def test_xau_xag_backtest_runs(self):
        if not os.path.exists(MT5_DB):
            pytest.skip("MT5-база отсутствует")
        pair = {"name": "XAU/XAG", "source": "mt5", "symbols": ["XAUUSD", "XAGUSD"]}
        cfg = dict(CFG)
        pa = PairAnalyzer(pair, cfg)
        p1 = pa._load_leg("XAUUSD", "D1")
        p2 = pa._load_leg("XAGUSD", "D1")
        from pairs_analysis import data as data_mod
        p1, p2 = data_mod.align(p1, p2)
        res = _engine().walk_forward(p1, p2, "XAU/XAG", "D1")
        s = res.summary()
        assert "avg_r" in s
        for t in res.trades:
            assert abs(t.entry_z) >= 2.0
            assert t.exit_reason in ("exit_z", "stop_z", "timeout", "end_of_data")
