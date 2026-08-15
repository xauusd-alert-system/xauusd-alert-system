"""
Tests for the Week-1 diagnostics (scripts/diag_r_metrics.py) and the
deflated-sharpe additions (cost stress, decision gate, IS->OOS slope).

Must run on mock data (no real SQLite required).
"""

import json
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.diag_r_metrics import _mfe_mae, _signal_mask, run_diagnostics
from scripts.deflated_sharpe import (
    _make_synthetic_wf_df,
    _inject_biased_probs,
    _apply_cost_mult,
    run_analysis,
    decision_gate,
)
from backtest.deflated_sharpe import cscv_pbo


@pytest.fixture(scope="module")
def synthetic_gbp_df():
    df = _make_synthetic_wf_df(12600, price=1.28, atr=0.0014, freq="1h")
    return _inject_biased_probs(df)


# ---------------------------------------------------------------------------
# MFE/MAE math
# ---------------------------------------------------------------------------

def test_mfe_mae_known_values():
    """Highs reach +2 steps on the NEXT bar, lows never dip: MFE=2, MAE=0."""
    df = pd.DataFrame({
        "high": [1.30, 1.30 + 2 * 0.0014, 1.30],
        "low": [1.30, 1.30, 1.30],
        "close": [1.30, 1.30, 1.30],
        "atr": [0.0014, 0.0014, 0.0014],
    })
    mm = _mfe_mae(df, horizon=2)
    assert mm["mfe"].iloc[0] == pytest.approx(2.0, abs=1e-6)
    assert mm["mae"].iloc[0] == pytest.approx(0.0, abs=1e-6)


def test_mfe_mae_excludes_signal_bar():
    """The signal bar's own high must NOT count (entry is at its close)."""
    df = pd.DataFrame({
        "high": [1.30 + 5 * 0.0014, 1.30, 1.30],
        "low": [1.30, 1.30, 1.30],
        "close": [1.30, 1.30, 1.30],
        "atr": [0.0014, 0.0014, 0.0014],
    })
    mm = _mfe_mae(df, horizon=2)
    assert mm["mfe"].iloc[0] == pytest.approx(0.0, abs=1e-9)


def test_signal_mask_blocks_low_prob():
    df = pd.DataFrame({
        "close": [1.3] * 3, "high": [1.3] * 3, "low": [1.3] * 3,
        "atr": [0.0014] * 3, "regime": ["trend_up"] * 3,
        "ml_p_long": [0.9, 0.5, 0.6], "ml_p_short": [0.1, 0.5, 0.4],
    })
    mask = _signal_mask(df)
    assert mask.tolist() == [True, False, True]


# ---------------------------------------------------------------------------
# run_diagnostics structure
# ---------------------------------------------------------------------------

def test_run_diagnostics_structure(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    d = run_diagnostics(cfg, "GBPUSD", synthetic_gbp_df, max_folds=4)
    assert d["n_folds"] == 4
    assert d["n_trades"] > 0
    assert d["n_features"] == 46
    assert d["events_per_feature"] > 0
    r = d["r_metrics"]
    assert r["n"] == d["n_trades"]
    assert r["avg_win_r"] > 0
    assert r["avg_loss_r"] > 0
    assert r["breakeven_wr_pct"] > 0
    assert len(r["buckets"]) > 0
    assert d["t_block"] is not None
    assert d["cost_ratio_pct"] is not None
    assert d["fold_sign_test"]["n_folds"] > 0
    assert d["mfe_mae"]  # per-regime MFE/MAE present
    json.dumps({k: v for k, v in d.items() if k != "trades"}, default=str)


def test_run_diagnostics_no_folds_raises():
    from config.loader import load_config
    cfg = load_config()
    df = _make_synthetic_wf_df(500, price=1.28, atr=0.0014, freq="1h")
    with pytest.raises(ValueError, match="No walk-forward folds"):
        run_diagnostics(cfg, "GBPUSD", df)


def test_main_writes_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from scripts.diag_r_metrics import main
    out = str(tmp_path / "diag.csv")
    main(["--asset", "GBPUSD", "--max-folds", "2", "--out", out])
    assert os.path.exists(out)
    df = pd.read_csv(out)
    assert {"pnl", "exit_reason", "entry_price", "initial_stop_price"} <= set(df.columns)
    with open(out.replace(".csv", ".json"), encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["asset"] == "GBPUSD"
    assert payload["synthetic"] is True


# ---------------------------------------------------------------------------
# deflated_sharpe: cost stress + gate + slope
# ---------------------------------------------------------------------------

def test_apply_cost_mult_scales_costs():
    from config.loader import load_config
    cfg = load_config()
    c2 = _apply_cost_mult(cfg, "GBPUSD", 1.5)
    a = c2["assets"]["GBPUSD"]
    assert a["spread_usd"] == pytest.approx(0.00015 * 1.5)
    assert a["slippage_usd"] == pytest.approx(0.0002 * 1.5)
    assert c2["backtest"]["commission_per_trade"] == pytest.approx(0.07 * 1.5)


def test_run_analysis_cost_stress_and_gate(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    res = run_analysis(cfg, "GBPUSD", synthetic_gbp_df,
                       variants={"current": {}, "null": None},
                       historical_trials=729, cost_stress=True)
    st = res["cost_stress"]
    assert st is not None
    assert st["cost_mult"] == 1.5
    assert st["n_trades"] > 0
    assert st["profit_factor"] > 0
    cur = next(t for t in res["trials"] if t["variant"] == "current")
    assert "t_block" in cur
    assert "valid_folds" in cur
    # A fold only votes with enough trades, so the vote denominator can never
    # exceed the number of folds that traded at all.
    assert cur["valid_folds"] <= cur["traded_folds"] <= cur["n_folds"]
    assert "median_fold_pnl" in cur
    assert "total_pnl_ex_best" in cur
    gate = decision_gate(res)
    assert set(gate["checks"]) == {
        "block_bootstrap_t >= 3.0", "DSR(N_eff) > 0.95", "PBO < 0.20",
        "PF > 1.1 at 1.5x costs",
        "folds: total PnL > 0, PnL ex-best fold > 0, 55% positive",
            "IS->OOS informativeness", "locked hold-out confirms"}
    assert gate["checks"]["locked hold-out confirms"] is None
    # The gate must publish WHICH leg of the fold condition failed; a bare
    # boolean is what let "57.1% positive folds" stand in for a result.
    fh = gate["fold_health"]
    assert {"total_pnl_positive", "ex_best_positive",
            "positive_share_ok", "passed"} <= set(fh)
    assert fh["passed"] == (fh["total_pnl_positive"] and fh["ex_best_positive"]
                            and fh["positive_share_ok"])


def test_run_analysis_no_cost_stress_flag(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    res = run_analysis(cfg, "GBPUSD", synthetic_gbp_df,
                       variants={"current": {}}, historical_trials=729,
                       cost_stress=False)
    assert res["cost_stress"] is None


def test_cscv_slope_present():
    rng = np.random.default_rng(21)
    M = rng.normal(0.0, 1.0, size=(6, 24))
    M[0, :] += 1.0
    res = cscv_pbo(M)
    assert "is_oos_slope" in res
    assert math.isfinite(res["is_oos_slope"]) or math.isnan(res["is_oos_slope"])
