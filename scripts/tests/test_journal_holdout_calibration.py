"""
Tests for the audit organizational tools:
- scripts/trial_journal.py (append-only journal, DSR N from journal, locked
  hold-out guard)
- scripts/exit_calibration.py (train-only MFE/MAE exit-geometry calibration)
- scripts/diag_entry_timing.py (look-ahead fill check)

Must run on mock data (no real SQLite required).
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.deflated_sharpe import _inject_biased_probs, _make_synthetic_wf_df
from scripts.diag_entry_timing import run_fill_modes
from scripts.exit_calibration import (
    calibrate_stop,
    calibrate_targets,
    run_calibration,
    trailing_decision,
)
from scripts.trial_journal import (
    count_trials,
    default_historical_trials,
    enforce_locked_holdout,
    locked_holdout_violations,
    log_trial,
    read_journal,
)

# ---------------------------------------------------------------------------
# Trial journal
# ---------------------------------------------------------------------------


def test_journal_append_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.trial_journal.JOURNAL_PATH", str(tmp_path / "trial_journal.csv"))
    log_trial("run_backtest", "GBPUSD", {"timeframe": "H1"}, {"median_pf": 2.42, "n_folds": 24})
    log_trial("grid_search_gbp", "GBPUSD", {"stop_mult": 3.0}, {"median_pf": 1.1})
    log_trial("deflated_sharpe", "EURUSD", {"historical_trials": 200}, {"pbo": 0.1})
    rows = read_journal()
    assert len(rows) == 3
    assert rows[0]["experiment"] == "run_backtest"
    assert json.loads(rows[0]["metrics_json"])["median_pf"] == 2.42
    assert count_trials("GBPUSD") == 2
    assert count_trials("EURUSD") == 1


def test_journal_default_historical_trials(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.trial_journal.JOURNAL_PATH", str(tmp_path / "trial_journal.csv"))
    # no journal yet -> floor 729
    assert default_historical_trials("GBPUSD") == 729
    for i in range(5):
        log_trial("grid_search_gbp", "GBPUSD", {"i": i}, {"median_pf": 1.0})
    # journal has 5 GBPUSD rows -> max(5, 729) stays 729 (grid history floor)
    assert default_historical_trials("GBPUSD") == 729


# ---------------------------------------------------------------------------
# Locked hold-out
# ---------------------------------------------------------------------------


def _windows():
    from backtest.walk_forward import WalkForwardWindow

    return [
        WalkForwardWindow(0, 10, 10, 20),
        WalkForwardWindow(10, 20, 20, 30),
        WalkForwardWindow(20, 30, 30, 40),
    ]


def test_locked_holdout_disabled_returns_empty():
    cfg = {"validation": {"locked_holdout": {"enabled": False, "start": None, "end": None}}}
    assert locked_holdout_violations(cfg, _windows()) == []


def test_locked_holdout_violations_detected():
    cfg = {
        "validation": {
            "locked_holdout": {"enabled": True, "start": "1970-01-01 00:00:21", "end": "1970-01-01 00:00:25"}
        }
    }
    bad = locked_holdout_violations(cfg, _windows())
    # windows: [10,20) no overlap, [20,30) overlaps [21,25], [30,40) no
    assert len(bad) == 1
    assert "test_start_utc" in bad[0]


def test_enforce_locked_holdout_raises_and_allows():
    cfg = {
        "validation": {
            "locked_holdout": {"enabled": True, "start": "1970-01-01 00:00:21", "end": "1970-01-01 00:00:25"}
        }
    }
    with pytest.raises(SystemExit, match="LOCKED HOLD-OUT VIOLATION"):
        enforce_locked_holdout(cfg, _windows(), "run_backtest")
    # --allow-locked proceeds
    enforce_locked_holdout(cfg, _windows(), "run_backtest", allow=True)


# ---------------------------------------------------------------------------
# Exit calibration math
# ---------------------------------------------------------------------------


def _mfe_mae_df(n=2000, seed=5):
    rng = np.random.default_rng(seed)
    regs = rng.choice(["trend_up", "range"], n)
    mfe = rng.gamma(2.2, 1.0, n)  # heavy right tail
    mae = rng.gamma(2.0, 1.0, n)
    return pd.DataFrame({"regime": regs, "mfe": mfe, "mae": mae})


def test_calibrate_stop_respects_probability_cap():
    d = _mfe_mae_df()
    r = calibrate_stop(d, max_p=0.20)
    assert r["sl_steps"] is not None
    # achieved conditional probability must be <= the cap
    reached = d[d["mfe"] >= 2.0]
    assert (reached["mae"] >= r["sl_steps"]).mean() <= 0.20 + 1e-9
    assert r["n_mfe2"] >= 10


def test_calibrate_stop_small_sample_nan():
    d = pd.DataFrame({"mfe": [0.1, 0.2], "mae": [0.1, 0.2]})
    r = calibrate_stop(d)
    assert r["sl_steps"] is None


def test_calibrate_targets_quantiles():
    mfe = np.arange(1.0, 101.0)  # uniform 1..100
    r = calibrate_targets(mfe, tp1_q=0.55, tp2_q=0.75)
    assert r["tp1_steps"] == pytest.approx(55.45, abs=0.5)  # ~q55 of 1..100
    assert r["tp2_steps"] == pytest.approx(75.25, abs=0.5)
    assert r["n"] == 100


def test_trailing_decision_thresholds():
    # strong tail -> trailing recommended
    strong = pd.DataFrame(
        {"regime": ["trend_up"] * 200, "mfe": np.concatenate([np.full(100, 3.0), np.full(60, 6.0), np.full(40, 8.0)])}
    )
    r = trailing_decision(strong)
    assert r["verdict"] == "trailing_recommended"
    # weak tail -> fixed TP3
    weak = pd.DataFrame({"regime": ["trend_up"] * 200, "mfe": np.full(200, 3.0)})
    assert trailing_decision(weak)["verdict"] == "fixed_tp3"


def test_run_calibration_structure_and_train_only():
    from config.loader import load_config

    cfg = load_config()
    df = _make_synthetic_wf_df(12600, price=1.28, atr=0.0014, freq="1h")
    df = _inject_biased_probs(df)
    d = run_calibration(cfg, "GBPUSD", df, max_folds=4)
    assert d["n_folds"] == 4
    assert d["n_signals"] > 0
    assert "trend_up" in d["per_regime"]
    assert "_trailing" in d["per_regime"]
    json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# Entry-timing (look-ahead) check
# ---------------------------------------------------------------------------


def test_run_fill_modes_structure():
    from config.loader import load_config

    cfg = load_config()
    df = _make_synthetic_wf_df(12600, price=1.28, atr=0.0014, freq="1h")
    df = _inject_biased_probs(df)
    d = run_fill_modes(cfg, "GBPUSD", df, max_folds=4)
    assert d["asset"] == "GBPUSD"
    for mode in ("next_open", "signal_close"):
        assert d[mode]["n_trades"] > 0
        assert "mean_r" in d[mode] and "t_block" in d[mode]
    assert d["gap_stats"]["n"] > 0
    assert d["gap_stats"]["mean_atr"] > 0


def test_run_fill_modes_no_folds_raises():
    from config.loader import load_config

    cfg = load_config()
    df = _make_synthetic_wf_df(500, price=1.28, atr=0.0014, freq="1h")
    with pytest.raises(ValueError, match="No walk-forward folds"):
        run_fill_modes(cfg, "GBPUSD", df)
