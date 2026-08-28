"""
Tests for the exit-path profiling tool (scripts/exit_profile.py).

Must run on mock data (no real SQLite required).
"""

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.deflated_sharpe import _inject_biased_probs, _make_synthetic_wf_df
from scripts.exit_profile import _aggregate, build_exit_profile, classify_path


def test_classify_path_buckets():
    assert classify_path("tp3_runner", True, True) == "TP3"
    assert classify_path("trailing", True, True) == "TP2_trailing"
    assert classify_path("stop", False, False) == "SL_pre_TP1"
    assert classify_path("stop", True, False) == "TP1_SL"
    assert classify_path("breakeven", False, False) == "BE_early"
    assert classify_path("breakeven", True, False) == "TP1_BE"
    assert classify_path("breakeven", True, True) == "TP2_exit"
    assert classify_path("timeout", True, False) == "TP1_timeout"
    assert classify_path("timeout", False, False) == "timeout_pre_tp1"
    assert classify_path("timeout", True, True) == "TP2_exit"
    assert classify_path("weird", False, False) == "other:weird"


def test_aggregate_payoff_geometry():
    tdf = pd.DataFrame(
        {
            "path": ["TP3", "TP3", "SL_pre_TP1"],
            "net_r": [1.7, 1.7, -3.0],
            "pnl": [1.7, 1.7, -3.0],
            "regime": ["trend_up"] * 3,
        }
    )
    agg = _aggregate(tdf)
    assert agg["n"] == 3
    assert agg["payoff"]["avg_win_R"] == pytest.approx(1.7)
    assert agg["payoff"]["avg_loss_R"] == pytest.approx(3.0)
    # BE_WR = 3 / (1.7 + 3) = 63.8% -- the audit's headline number for a 1:3 grid
    assert agg["payoff"]["breakeven_wr_pct"] == pytest.approx(63.8, abs=0.1)
    assert agg["payoff"]["actual_wr_pct"] == pytest.approx(66.7, abs=0.1)


@pytest.fixture(scope="module")
def synthetic_gbp_df():
    df = _make_synthetic_wf_df(12600, price=1.28, atr=0.0014, freq="1h")
    return _inject_biased_probs(df)


def test_build_exit_profile_structure(synthetic_gbp_df):
    from config.loader import load_config

    cfg = load_config()
    prof = build_exit_profile(cfg, "GBPUSD", synthetic_gbp_df, max_folds=4)
    assert prof["n_folds"] == 4
    assert prof["n_trades"] > 0
    assert set(["fold", "regime", "exit_reason", "path", "net_r", "pnl"]) <= set(prof["trades"].columns)
    assert prof["overall"]["n"] == prof["n_trades"]
    assert "TP3" in prof["overall"]["paths"] or "SL_pre_TP1" in prof["overall"]["paths"]
    json.dumps({k: v for k, v in prof.items() if k != "trades"})  # serializable


def test_build_exit_profile_no_folds_raises():
    from config.loader import load_config

    cfg = load_config()
    df = _make_synthetic_wf_df(500, price=1.28, atr=0.0014, freq="1h")
    with pytest.raises(ValueError, match="No walk-forward folds"):
        build_exit_profile(cfg, "GBPUSD", df)


def test_main_writes_csv_and_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from scripts.exit_profile import main

    out = str(tmp_path / "ep.csv")
    main(["--asset", "GBPUSD", "--max-folds", "2", "--out", out])
    assert os.path.exists(out)
    df = pd.read_csv(out)
    assert {"path", "net_r", "pnl", "exit_reason", "regime"} <= set(df.columns)
    assert os.path.exists(out.replace(".csv", ".json"))
    with open(out.replace(".csv", ".json"), encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["asset"] == "GBPUSD"
    assert payload["synthetic"] is True
