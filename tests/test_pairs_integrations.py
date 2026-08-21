# -*- coding: utf-8 -*-
"""Tests for pairs_analysis.integrations (ТЗ §4.6-§4.8)."""
import csv
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

from pairs_analysis import (
    load_config, PairAnalyzer, SignalEngine, EnsembleEngine,
)
from pairs_analysis.integrations import (
    scan_pairs, pair_score, PairWatchlistEntry,
    size_pair_position, PairPosition,
    log_pair_trade, read_pair_journal, pair_weekly_metrics,
    pair_cumulative_stats, PAIR_JOURNAL_FIELDS,
)
from pairs_analysis.signal import Signal
from pairs_analysis.ensemble import EnsembleForecast, EngineResult

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MT5_DB = os.path.join(ROOT, "data", "market_data_mt5.sqlite")


class TestScanPairs:
    @pytest.mark.skipif(not os.path.exists(MT5_DB), reason="MT5-база отсутствует")
    def test_returns_all_pairs(self):
        cfg = load_config()
        entries = scan_pairs(cfg, timeframe="D1")
        assert len(entries) == len(cfg["pairs"])
        for e in entries:
            assert e.status in ("VALID_MEANREV", "NO_EDGE", "INVALID")
            assert e.direction in ("long", "short", "none")

    @pytest.mark.skipif(not os.path.exists(MT5_DB), reason="MT5-база отсутствует")
    def test_xau_xag_is_valid(self):
        cfg = load_config()
        entries = scan_pairs(cfg, timeframe="D1")
        xau = next(e for e in entries if e.name == "XAU/XAG")
        # XAU/XAG is co-integrated (ADF p ≈ 0) — should not be INVALID
        assert xau.status != "INVALID"
        assert xau.adf_p < 0.05

    @pytest.mark.skipif(not os.path.exists(MT5_DB), reason="MT5-база отсутствует")
    def test_sorted_by_score(self):
        cfg = load_config()
        entries = scan_pairs(cfg, timeframe="D1")
        scores = [e.score for e in entries]
        assert scores == sorted(scores, reverse=True)


class TestPairScore:
    def test_score_range(self):
        """Score should be between 0 and ~1."""
        m = type("M", (), {
            "z": pd.Series([0, 0, 2.5]), "adf_p": 0.01, "hurst": 0.4,
        })()
        sig = Signal("TEST", "D1", "2025-01-01", 2.5, "short", "test", 0.01, 5.0, 0.4, True)
        ens = EnsembleForecast("TEST", "D1", "2025-01-01", "short", 70.0)
        sc = pair_score(m, sig, ens)
        assert 0 <= sc <= 2.0  # z_norm can exceed 1 if |z| > 2*entry_z


class TestSizePairPosition:
    def test_basic_sizing(self):
        m = type("M", (), {
            "name": "XAU/XAG", "beta": 2.0, "sigma": 0.04,
            "p1_last": 4400.0, "p2_last": 65.0, "half_life_days": 2.0,
            "spread": pd.Series([0.01, -0.02, 0.03]),
        })()
        sig = Signal("XAU/XAG", "D1", "2025-01-01", -2.3, "long", "test", 0.01, 2.0, 0.4, True)
        pos = size_pair_position(m, sig, risk_usd=5.0)
        assert pos.pair_name == "XAU/XAG"
        assert pos.direction == "long"
        assert pos.risk_usd > 0
        assert pos.p1_contracts > 0
        assert pos.p2_contracts > 0
        assert pos.hedge_ratio == 2.0
        assert pos.vol_adjustment == 1.0

    def test_vol_adjustment(self):
        m = type("M", (), {
            "name": "BTC/ETH", "beta": 1.5, "sigma": 0.02,
            "p1_last": 75000.0, "p2_last": 2500.0, "half_life_days": 1.5,
            "spread": pd.Series([0.01]),
        })()
        sig = Signal("BTC/ETH", "D1", "2025-01-01", -2.1, "long", "test", 0.01, 1.5, 0.4, True)
        pos = size_pair_position(m, sig, risk_usd=5.0, target_spread_vol=0.06)
        assert 0.5 <= pos.vol_adjustment <= 1.5

    def test_as_dict(self):
        m = type("M", (), {
            "name": "TEST", "beta": 1.0, "sigma": 0.05,
            "p1_last": 100.0, "p2_last": 50.0, "half_life_days": 10.0,
            "spread": pd.Series([0.01]),
        })()
        sig = Signal("TEST", "D1", "2025-01-01", 2.5, "short", "test", 0.01, 10.0, 0.4, True)
        pos = size_pair_position(m, sig, risk_usd=5.0)
        d = pos.as_dict()
        assert "pair_name" in d
        assert "p1_contracts" in d


class TestJournal:
    def test_log_and_read(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            path = f.name
        try:
            num = log_pair_trade(
                path, "2025-01-15", "14:30", "XAU/XAG", "long", "spread",
                entry_z=-2.3, exit_z=0.1, exit_reason="exit_z",
                r=3.5, bars_held=5, beta=2.0, hedge_mode="dollar_neutral",
                risk_usd=5.0, p1_symbol="XAUUSD", p1_contracts=0.01,
                p2_symbol="XAGUSD", p2_contracts=0.02,
                adf_p=0.001, half_life_days=2.0, hurst=0.4,
                regime="mean-reverting", ensemble_direction="long",
                ensemble_confidence=65.0)
            assert num == 1
            rows = read_pair_journal(path)
            assert len(rows) == 1
            assert rows[0]["pair"] == "XAU/XAG"
            assert rows[0]["r"] == "3.5"
            assert rows[0]["exit_reason"] == "exit_z"
        finally:
            os.unlink(path)

    def test_cumulative_stats(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            path = f.name
        try:
            for i, r in enumerate([3.5, -1.0, 2.0, 4.0, -1.0]):
                log_pair_trade(
                    path, f"2025-01-{15+i}", "14:30", "XAU/XAG", "long", "spread",
                    entry_z=-2.3, exit_z=0.1, exit_reason="exit_z",
                    r=r, bars_held=5, beta=2.0, hedge_mode="dollar_neutral",
                    risk_usd=5.0, p1_symbol="XAUUSD", p1_contracts=0.01,
                    p2_symbol="XAGUSD", p2_contracts=0.02,
                    adf_p=0.001, half_life_days=2.0, hurst=0.4,
                    regime="mean-reverting", ensemble_direction="long",
                    ensemble_confidence=65.0)
            stats = pair_cumulative_stats(path)
            assert stats["total_trades"] == 5
            assert stats["wins"] == 3
            assert stats["losses"] == 2
            assert stats["avg_r"] == pytest.approx(1.5, abs=0.1)
        finally:
            os.unlink(path)

    def test_weekly_metrics(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            path = f.name
        try:
            for i, r in enumerate([3.5, -1.0, 2.0]):
                log_pair_trade(
                    path, f"2025-01-{13+i}", "14:30", "BTC/ETH", "long", "spread",
                    entry_z=-2.1 + i * 0.1, exit_z=0.0, exit_reason="exit_z",
                    r=r, bars_held=3, beta=1.5, hedge_mode="dollar_neutral",
                    risk_usd=5.0, p1_symbol="BTCUSDT", p1_contracts=0.001,
                    p2_symbol="ETHUSDT", p2_contracts=0.01,
                    adf_p=0.001, half_life_days=1.5, hurst=0.4,
                    regime="mean-reverting", ensemble_direction="long",
                    ensemble_confidence=70.0)
            weeks = pair_weekly_metrics(path)
            assert len(weeks) >= 1
            w = weeks[0]
            assert w["trades"] == 3
            assert w["wins"] == 2
            assert w["avg_r"] is not None
        finally:
            os.unlink(path)
