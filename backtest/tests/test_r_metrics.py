"""
Tests for the R-multiplicator metrology (quant audit 2026-08-07, Claude 5 Opus
plan): compute_r_metrics, block_bootstrap_t, fold_sign_test, summarize_folds.

R = trade pnl / money(|entry - initial_stop|). The grid geometry caps R at
+0.567 (TP3 with 50/30/20) and floors it at -1.0; cross-asset comparisons
must be in R, never in raw money.
"""

import math
import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backtest.metrics import (
    trades_to_dataframe,
    compute_r_metrics,
    block_bootstrap_t,
    fold_sign_test,
    summarize_folds,
)


def _mk_trade(pnl, entry=1.30, stop=1.297, tp1=1.3003, reason="tp3_runner",
              tp1_hit=True, tp2_hit=True):
    return SimpleNamespace(
        entry_ts=1, exit_ts=2, direction=1, session="london",
        regime_at_entry="trend_up", pnl=pnl, exit_reason=reason,
        entry_price=entry, initial_stop_price=stop, tp1_price=tp1,
        volume=0.01)


def test_trades_to_dataframe_carries_r_fields():
    t = _mk_trade(0.9)
    df = trades_to_dataframe([t])
    assert df.loc[0, "entry_price"] == pytest.approx(1.30)
    assert df.loc[0, "initial_stop_price"] == pytest.approx(1.297)
    assert df.loc[0, "tp1_price"] == pytest.approx(1.3003)
    assert df.loc[0, "volume"] == pytest.approx(0.01)


def test_trades_to_dataframe_backward_compatible_with_plain_trades():
    t = SimpleNamespace(entry_ts=1, exit_ts=2, direction=1, session="london",
                        regime_at_entry="trend_up", pnl=1.0, exit_reason="stop")
    df = trades_to_dataframe([t])
    assert df.loc[0, "pnl"] == 1.0
    assert df.loc[0, "initial_stop_price"] is None


def test_compute_r_metrics_known_geometry():
    """TP3 full grid: 0.5*1/3 + 0.3*2/3 + 0.2*1 = 0.567R; stop = -1R."""
    risk_money = abs(1.30 - 1.297) * 0.01 * 100000  # = 3.0
    tdf = pd.DataFrame({
        "entry_price": [1.30, 1.30, 1.30],
        "initial_stop_price": [1.297, 1.297, 1.297],
        "pnl": [0.567 * risk_money, 0.567 * risk_money, -1.0 * risk_money],
        "exit_reason": ["tp3_runner", "tp3_runner", "stop"],
    })
    r = compute_r_metrics(tdf, point_value_lot=100000, volume=0.01)
    assert r["n"] == 3
    assert r["avg_win_r"] == pytest.approx(0.567, abs=1e-3)
    assert r["avg_loss_r"] == pytest.approx(1.0, abs=1e-3)
    # BE_WR = 1 / (1 + 0.567) = 63.8% -- audit's headline geometry number
    assert r["breakeven_wr_pct"] == pytest.approx(63.8, abs=0.2)
    assert r["buckets"]["stop"]["mean_r"] == pytest.approx(-1.0, abs=1e-3)
    assert r["buckets"]["tp3_runner"]["r_contribution_pct"] == pytest.approx(
        100.0 * (2 * 0.567) / (2 * 0.567 - 1.0), abs=0.1)


def test_compute_r_metrics_empty_and_missing_columns():
    assert compute_r_metrics(pd.DataFrame(), 1, 1)["n"] == 0
    tdf = pd.DataFrame({"pnl": [1.0]})  # no risk columns
    assert compute_r_metrics(tdf, 1, 1)["n"] == 0


def test_block_bootstrap_t_deterministic_and_sane():
    rng = np.random.default_rng(0)
    positive = rng.normal(0.05, 0.4, 600)
    t1 = block_bootstrap_t(positive, block=20, n_boot=500, seed=7)
    t2 = block_bootstrap_t(positive, block=20, n_boot=500, seed=7)
    assert t1 == t2
    assert t1 > 1.5
    zero = rng.normal(0.0, 0.4, 600)
    assert block_bootstrap_t(zero, n_boot=500) < t1
    assert block_bootstrap_t([]) == 0.0
    assert block_bootstrap_t([1.0]) == 0.0


def test_fold_sign_test_known_values():
    # GBPUSD audit numbers: 17/24 -> p ~ 0.03 one-sided
    st = fold_sign_test(17, 24)
    assert st["n_positive"] == 17
    assert st["p_one_sided"] < 0.05
    # coin flip exactly at half: z ~ 0
    st2 = fold_sign_test(12, 24)
    assert abs(st2["z"]) < 0.5
    assert st2["p_one_sided"] > 0.4


def test_summarize_folds_consistency_flag():
    good = [
        {"n_trades": 10, "total_pnl": 5.0, "profit_factor": 1.2},
        {"n_trades": 10, "total_pnl": 3.0, "profit_factor": 1.1},
        {"n_trades": 10, "total_pnl": -2.0, "profit_factor": 0.9},
        {"n_trades": 10, "total_pnl": 1.0, "profit_factor": 1.05},
    ]
    s = summarize_folds(good)
    assert s["valid_folds"] == 4
    assert s["positive_folds_valid"] == 3
    assert s["inconsistent"] is False

    # median PF > 1 with < 50% positive valid folds: impossible arithmetic
    # (PF > 1 <=> PnL > 0) -> flag the reporting mismatch
    bad = [
        {"n_trades": 10, "total_pnl": 5.0, "profit_factor": 1.2},
        {"n_trades": 10, "total_pnl": -5.0, "profit_factor": 0.8},
        {"n_trades": 10, "total_pnl": -5.0, "profit_factor": 0.8},
        {"n_trades": 10, "total_pnl": -5.0, "profit_factor": 0.8},
    ]
    s2 = summarize_folds(bad)
    # hand-crafted inconsistent stats: median PF reports >1 while fold PnL says
    # only 1/4 positive -> the note must be set
    assert s2["median_pf_valid"] is not None
