# -*- coding: utf-8 -*-
"""Парный статистический анализ (ТЗ): коинтеграция, спред, z-score, ADF,
half-life, режим, ensemble, интеграции. Этапы 1-5."""
from . import data, metrics
from .analyzer import PairAnalyzer, PairMetrics, analyze_all
from .config import load_config
from .signal import SignalEngine, Signal, PairTrade, BacktestResult
from .ensemble import EnsembleEngine, EnsembleForecast, EngineResult
from .integrations import (
    PairWatchlistEntry, scan_pairs, pair_score,
    PairPosition, size_pair_position,
    log_pair_trade, read_pair_journal, pair_weekly_metrics, pair_cumulative_stats,
)

__all__ = ["data", "metrics", "PairAnalyzer", "PairMetrics",
           "analyze_all", "load_config", "SignalEngine", "Signal",
           "PairTrade", "BacktestResult",
           "EnsembleEngine", "EnsembleForecast", "EngineResult",
           "PairWatchlistEntry", "scan_pairs", "pair_score",
           "PairPosition", "size_pair_position",
           "log_pair_trade", "read_pair_journal",
           "pair_weekly_metrics", "pair_cumulative_stats"]
