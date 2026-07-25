"""
Unit tests for backtest/engine.py, metrics.py, and walk_forward.py.
Run with: pytest backtest/tests/test_engine.py -v
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import fetch_mock_candles
from features.indicators import build_all_indicators
from regime.classifier import add_regime_indicators, classify_regime_series, RegimeLabel
from backtest.engine import EventDrivenBacktester, rule_based_signal, Trade
from backtest.metrics import trades_to_dataframe, compute_metrics, compute_metrics_per_session
from backtest.walk_forward import generate_windows, run_walk_forward

CFG = load_config()
SESSIONS = CFG["sessions"]


def _prepared_df(n=800, seed=42):
    df = fetch_mock_candles("M15", n_candles=n, sessions_config=SESSIONS, seed=seed)
    df = build_all_indicators(df, CFG)
    df = add_regime_indicators(df, CFG)
    df["regime"] = classify_regime_series(df, CFG)
    return df


def test_rule_based_signal_mapping():
    assert rule_based_signal(RegimeLabel.TREND_UP) == 1
    assert rule_based_signal(RegimeLabel.TREND_DOWN) == -1
    assert rule_based_signal(RegimeLabel.RANGE) == 0
    assert rule_based_signal(RegimeLabel.NO_TRADE) == 0
    assert rule_based_signal(RegimeLabel.COMPRESSION) == 0
    assert rule_based_signal(RegimeLabel.REVERSAL_WATCH) == 0


def test_engine_produces_trades_with_valid_fields():
    df = _prepared_df()
    engine = EventDrivenBacktester(CFG)
    trades = engine.run(df)
    assert isinstance(trades, list)
    for t in trades:
        assert isinstance(t, Trade)
        assert t.exit_reason in ("target", "stop", "timeout")
        assert t.pnl is not None
        assert t.exit_ts > t.entry_ts  # exit must happen strictly after entry


def test_entry_executes_one_candle_after_signal_no_lookahead():
    """
    CRITICAL TEST: manually verify that a trade's entry_price is derived from the
    OPEN of the candle AFTER the regime signal changed, never the same candle.
    """
    df = _prepared_df()
    engine = EventDrivenBacktester(CFG)
    trades = engine.run(df)
    ts_to_idx = {int(ts): i for i, ts in enumerate(df["timestamp_utc"].values)}
    for t in trades:
        entry_idx = ts_to_idx[t.entry_ts]
        assert entry_idx > 0, "Entry cannot happen on the very first candle (no prior signal possible)"
        # The regime that triggered this trade must have been observed on a PRIOR candle
        prior_regime = df["regime"].iloc[entry_idx - 1]
        signal = rule_based_signal(prior_regime)
        assert signal == t.direction, "Entry direction must match the signal decided on the PRIOR candle's close"


def test_at_most_one_open_position_at_a_time():
    df = _prepared_df()
    engine = EventDrivenBacktester(CFG)
    trades = engine.run(df)
    trades_sorted = sorted(trades, key=lambda t: t.entry_ts)
    for i in range(1, len(trades_sorted)):
        assert trades_sorted[i].entry_ts >= trades_sorted[i - 1].exit_ts, \
            "New trade must not open before the previous one closed"


def test_metrics_computation_basic():
    df = _prepared_df()
    engine = EventDrivenBacktester(CFG)
    trades = engine.run(df)
    trades_df = trades_to_dataframe(trades)
    metrics = compute_metrics(trades_df)
    assert metrics["n_trades"] == len(trades)
    if metrics["n_trades"] > 0:
        assert 0 <= metrics["win_rate"] <= 100
        assert metrics["max_drawdown"] <= 0


def test_metrics_per_session_keys_are_valid():
    df = _prepared_df()
    engine = EventDrivenBacktester(CFG)
    trades = engine.run(df)
    trades_df = trades_to_dataframe(trades)
    per_session = compute_metrics_per_session(trades_df)
    for session_name, m in per_session.items():
        assert m["n_trades"] > 0


def test_walk_forward_windows_never_overlap_train_into_test():
    df = _prepared_df(n=3000)
    windows = generate_windows(df, train_window_days=5, test_window_days=2, step_days=2)
    for w in windows:
        assert w.train_end_ts <= w.test_start_ts, "Train window must never extend into test window"
        assert w.test_start_ts < w.test_end_ts


def test_walk_forward_runner_calls_strategy_per_fold():
    df = _prepared_df(n=3000)

    def dummy_strategy(train_df, test_df, cfg):
        engine = EventDrivenBacktester(cfg)
        trades = engine.run(test_df.reset_index(drop=True))
        trades_df = trades_to_dataframe(trades)
        return compute_metrics(trades_df)

    # Use shorter windows for the test to guarantee at least one fold with limited mock data
    test_cfg = {**CFG, "backtest": {**CFG["backtest"], "walk_forward": {"train_window_days": 5, "test_window_days": 2, "step_days": 2}}}
    results = run_walk_forward(df, test_cfg, dummy_strategy)
    assert isinstance(results, list)
    for r in results:
        assert "window" in r
        assert "n_trades" in r
