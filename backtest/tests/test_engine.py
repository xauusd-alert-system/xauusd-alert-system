"""
Unit tests for backtest/engine.py, metrics.py, and walk_forward.py.
Run with: pytest backtest/tests/test_engine.py -v
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backtest.engine import EventDrivenBacktester, Trade, rule_based_signal
from backtest.metrics import (
    compute_metrics,
    compute_metrics_per_session,
    trades_to_dataframe,
)
from backtest.walk_forward import generate_windows, run_walk_forward
from config.loader import load_config
from data.ingestion import fetch_mock_candles, to_epoch_seconds
from features.indicators import build_all_indicators
from regime.classifier import RegimeLabel, add_regime_indicators, classify_regime_series

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
        assert trades_sorted[i].entry_ts >= trades_sorted[i - 1].exit_ts, (
            "New trade must not open before the previous one closed"
        )


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
    test_cfg = {
        **CFG,
        "backtest": {
            **CFG["backtest"],
            "walk_forward": {
                "train_window_days": 5,
                "test_window_days": 2,
                "step_days": 2,
            },
        },
    }
    results = run_walk_forward(df, test_cfg, dummy_strategy)
    assert isinstance(results, list)
    for r in results:
        assert "window" in r
        assert "n_trades" in r


def test_engine_barriers_follow_signal_grid():
    """The engine's ATR-scaled barriers must mirror the signal grid (asymmetric
    spec: stop = 2*step, TP1 = 1*step -> stop/target distance ratio = 2.0),
    not the training-label barriers (target 1.2 / stop 1.0)."""
    df = _prepared_df()
    engine = EventDrivenBacktester(CFG)
    trades = engine.run(df)
    assert len(trades) > 0
    for t in trades:
        stop_dist = abs(t.stop_price - t.entry_price)
        target_dist = abs(t.target_price - t.entry_price)
        if stop_dist == 0:
            # The stop was moved to entry by the early-breakeven rule, so the
            # 3:1 grid ratio no longer applies to this (already-protected) trade.
            continue
        assert stop_dist > 0 and target_dist > 0
        # Both barriers are sized off the SAME ATR at entry, so the ratio is
        # exactly stop_mult / tp1_mult = 2.0 for the shipped asymmetric grid
        # (owner request 2026-08-11: TP1=1, stop=2).
        assert np.isclose(stop_dist / target_dist, 2.0, rtol=1e-6), (
            f"expected stop/target = 2.0 (asymmetric signal grid), got {stop_dist / target_dist:.4f}"
        )


def test_engine_early_breakeven_limits_loss():
    """With signal_grid.breakeven_trigger_atr=0.5 the engine moves the stop to
    entry after the +0.6-step probe, so the -3.5-step drop that follows exits
    as a scratch (pnl ~ 0) instead of a full 3-step stop loss. The engine's
    exit_reason labels are fixed, so it still reads 'stop'."""
    cfg = {
        "backtest": {
            "spread_points": 0,
            "slippage_points": 0,
            "initial_balance": 100.0,
            "risk_per_trade_pct": 2.0,
        },
        "labeling": {
            "method": "atr_scaled",
            "horizon_candles_n": 36,
            "atr_column": "atr",
            "target_pips_x": 0.0,
            "stop_pips_y": 0.0,
        },
        "signal_grid": {
            "tp1_mult": 1.0,
            "stop_mult": 3.0,
            "breakeven_trigger_atr": 0.5,
        },
    }
    price = 1.10
    step = 0.0003
    idx = pd.date_range("2024-01-01", periods=10, freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp_utc": to_epoch_seconds(idx),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "session": "london",
            "regime": [RegimeLabel.TREND_UP] * 10,
            "atr": step,
        }
    )
    df.loc[2, "high"] = price + 0.6 * step  # probe above the 0.5-step BE trigger
    df.loc[3, "low"] = price - 3.5 * step  # would-be stop-out below -3*step

    engine = EventDrivenBacktester(cfg)
    trades = engine.run(df)
    assert len(trades) >= 1
    for t in trades:
        assert t.exit_reason == "stop"
        assert t.pnl > -0.0005  # scratched at breakeven, not a full 3-step loss


def test_walk_forward_purges_train_rows_whose_labels_overlap_test():
    """W3: train rows whose triple-barrier label window reaches into the test
    window must be purged (their future information would otherwise leak)."""
    df = _prepared_df(n=3000)

    def spy(train_df, test_df, cfg):
        return {"n_trades": 0, "total_pnl": 0.0, "profit_factor": 1.0}

    test_cfg = {
        **CFG,
        "backtest": {
            **CFG["backtest"],
            "walk_forward": {"train_window_days": 5, "test_window_days": 2, "step_days": 2},
        },
    }
    results = run_walk_forward(df, test_cfg, spy)
    assert results
    first = results[0]
    w = first["window"]
    full_train = df[(df["timestamp_utc"] >= w.train_start_ts) & (df["timestamp_utc"] < w.train_end_ts)]
    # The purged train set must be strictly smaller than the full window.
    assert first["purged_train_rows"] < len(full_train)
    assert first["purged_train_rows"] > 0
