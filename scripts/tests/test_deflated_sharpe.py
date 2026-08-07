"""
Tests for the Deflated Sharpe Ratio / CSCV PBO module (backtest/deflated_sharpe.py)
and the per-asset assessment script (scripts/deflated_sharpe.py).

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

from backtest.deflated_sharpe import (
    annualized_sharpe,
    probabilistic_sharpe_ratio,
    expected_max_sharpe,
    deflated_sharpe_ratio,
    minimum_track_record_length,
    cscv_pbo,
    _pick_n_splits,
)
from scripts.deflated_sharpe import (
    run_analysis,
    _make_synthetic_wf_df,
    _inject_biased_probs,
)


# ---------------------------------------------------------------------------
# PSR / DSR math
# ---------------------------------------------------------------------------


def test_annualized_sharpe_known_value():
    # mean=3, std(ddof=1)=sqrt(2.5)=1.5811 -> 3/1.5811*sqrt(250) = 30.0
    assert annualized_sharpe([1, 2, 3, 4, 5]) == pytest.approx(30.0, abs=1e-9)


def test_annualized_sharpe_degenerate():
    assert annualized_sharpe([]) == 0.0
    assert annualized_sharpe([1.0]) == 0.0
    assert annualized_sharpe([2.0, 2.0, 2.0]) == 0.0  # zero std


def test_psr_zero_sharpe_is_half():
    """An exactly symmetric zero-mean sample must give PSR(0) ~= 0.5."""
    rng = np.random.default_rng(1)
    half = rng.normal(0.0, 1.0, 400)
    pnls = np.concatenate([half, -half])  # mean == 0 by construction
    psr = probabilistic_sharpe_ratio(pnls, sr_benchmark=0.0)
    assert 0.4 < psr < 0.6


def test_psr_increases_with_sharpe():
    rng = np.random.default_rng(2)
    weak = rng.normal(0.02, 1.0, 300)
    strong = rng.normal(0.15, 1.0, 300)
    assert probabilistic_sharpe_ratio(strong, 0.0) > probabilistic_sharpe_ratio(weak, 0.0)


def test_expected_max_sharpe_monotonic_in_trials():
    sr, skew, kurt, n = 0.5, 0.0, 0.0, 100
    assert expected_max_sharpe(2, sr, skew, kurt, n) < expected_max_sharpe(100, sr, skew, kurt, n)
    assert expected_max_sharpe(100, sr, skew, kurt, n) < expected_max_sharpe(10000, sr, skew, kurt, n)


def test_expected_max_sharpe_single_trial_zero():
    assert expected_max_sharpe(1, 0.5, 0.0, 0.0, 100) == 0.0


def test_dsr_single_trial_equals_psr_zero():
    """With n_trials=1 there is nothing to deflate: DSR == PSR(0)."""
    rng = np.random.default_rng(3)
    pnls = rng.normal(0.05, 1.0, 500)
    d = deflated_sharpe_ratio(pnls, n_trials=1)
    assert d["expected_max_sr"] == pytest.approx(0.0)
    assert d["dsr"] == pytest.approx(probabilistic_sharpe_ratio(pnls, 0.0), abs=1e-9)


def test_dsr_deflates_with_trials():
    """More trials tried -> lower DSR for the same observed PnL stream."""
    rng = np.random.default_rng(4)
    pnls = rng.normal(0.01, 1.0, 800)  # weak edge: deflation visibly matters
    d1 = deflated_sharpe_ratio(pnls, n_trials=5)
    d2 = deflated_sharpe_ratio(pnls, n_trials=729)
    assert d1["dsr"] > d2["dsr"]
    assert d2["expected_max_sr"] > d1["expected_max_sr"]


def test_min_trl_increases_with_trials_and_prob():
    rng = np.random.default_rng(5)
    pnls = rng.normal(0.08, 1.0, 1000)
    m1 = minimum_track_record_length(pnls, n_trials=5)
    m2 = minimum_track_record_length(pnls, n_trials=729)
    assert m1["min_trl_trades"] < m2["min_trl_trades"]
    m3 = minimum_track_record_length(pnls, n_trials=729, prob=0.99)
    assert m3["min_trl_trades"] > m2["min_trl_trades"]


def test_min_trl_infinite_for_negative_edge():
    pnls = -np.abs(np.random.default_rng(6).normal(0.0, 1.0, 200)) - 0.01
    m = minimum_track_record_length(pnls, n_trials=729)
    assert math.isinf(m["min_trl_trades"])


# ---------------------------------------------------------------------------
# CSCV PBO
# ---------------------------------------------------------------------------


def test_cscv_dominant_strategy_pbo_zero():
    """One strategy dominates every fold -> IS-best is always the OOS best too."""
    rng = np.random.default_rng(7)
    M = rng.normal(0.0, 0.5, size=(5, 24))
    M[0, :] += 2.0  # trial 0 dominates everywhere
    res = cscv_pbo(M, random_seed=42)
    assert res["pbo"] == 0.0
    assert res["mean_lambda"] > 0.0


def test_cscv_random_matrix_pbo_around_half():
    """IID noise across trials: the IS-best trial is ~random OOS (PBO ~ 0.5)."""
    rng = np.random.default_rng(8)
    M = rng.normal(0.0, 1.0, size=(8, 24))
    res = cscv_pbo(M, random_seed=42)
    assert 0.2 < res["pbo"] < 0.8
    assert -1.0 < res["mean_lambda"] < 1.0
    assert res["n_trials"] == 8
    assert res["n_splits"] == 12  # exact divisor of 24


def test_cscv_deterministic_with_seed():
    rng = np.random.default_rng(9)
    M = rng.normal(0.0, 1.0, size=(6, 20))
    r1 = cscv_pbo(M, max_combinations=30, random_seed=123)
    r2 = cscv_pbo(M, max_combinations=30, random_seed=123)
    r3 = cscv_pbo(M, max_combinations=30, random_seed=999)
    assert r1 == r2
    assert r1["pbo"] != r3["pbo"]  # different seeds sample different combos


def test_pick_n_splits_prefers_exact_divisor():
    assert _pick_n_splits(24) == 12   # exact divisors 4,6,8,12 -> largest
    assert _pick_n_splits(42) == 14   # exact divisors 6,14 -> largest
    assert _pick_n_splits(14) == 14
    assert _pick_n_splits(26) == 12   # no exact divisor: min truncation (26%12=2), tie -> larger
    assert _pick_n_splits(6) == 6


def test_cscv_warns_below_four_trials():
    rng = np.random.default_rng(10)
    M = rng.normal(0.0, 1.0, size=(3, 24))
    with pytest.warns(RuntimeWarning):
        cscv_pbo(M)


# ---------------------------------------------------------------------------
# Script integration (synthetic data, no DB)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synthetic_gbp_df():
    """~500 days of H1 rows -> 4 walk-forward folds, biased probs injected."""
    df = _make_synthetic_wf_df(12600, price=1.28, atr=0.0014, freq="1h")
    return _inject_biased_probs(df)


def test_run_analysis_synthetic_structure(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    variants = {"current": {},
                "tight": {"signal_grid": {"stop_mult": 2.0, "breakeven_trigger_atr": 0.5}},
                "wide": {"signal_grid": {"stop_mult": 4.0, "breakeven_trigger_atr": 1.0, "tp3_mult": 4.0}},
                "null": None,
                }
    res = run_analysis(cfg, "GBPUSD", synthetic_gbp_df, variants=variants,
                       historical_trials=729)
    assert res["n_folds"] == 4
    assert res["n_trials"] == 4
    assert len(res["trials"]) == 4
    # JSON-serializable (plain python types)
    json.dumps(res)
    for t in res["trials"]:
        assert 0.0 <= t["dsr_trials"] <= 1.0 or math.isnan(t["dsr_trials"])
        assert 0.0 <= t["dsr_historical"] <= 1.0 or math.isnan(t["dsr_historical"])
        assert t["n_folds"] == 4
    c = res["cscv"]
    assert 0.0 <= c["pbo"] <= 1.0
    assert c["n_trials"] == 4


def test_run_analysis_biased_beats_null(synthetic_gbp_df):
    """The injected-bias current config must DOMINATE the random-prob null:
    more trades, higher Sharpe, higher PnL. This proves the machinery detects
    a real (here: leaked) edge instead of calling everything random."""
    from config.loader import load_config
    cfg = load_config()
    res = run_analysis(cfg, "GBPUSD", synthetic_gbp_df,
                       variants={"current": {}, "null": None},
                       historical_trials=729)
    cur = next(t for t in res["trials"] if t["variant"] == "current")
    null = next(t for t in res["trials"] if t["variant"] == "null")
    assert cur["n_trades"] > 0 and null["n_trades"] > 0
    assert cur["n_trades"] > null["n_trades"]
    assert cur["sharpe"] > null["sharpe"]
    assert cur["total_pnl"] > null["total_pnl"]
    assert cur["dsr_historical"] >= null["dsr_historical"]


def test_run_analysis_deterministic(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    variants = {"current": {}, "null": None}
    r1 = run_analysis(cfg, "GBPUSD", synthetic_gbp_df, variants=variants)
    r2 = run_analysis(cfg, "GBPUSD", synthetic_gbp_df, variants=variants)
    # NaN-tolerant comparison (floats may be nan, and nan != nan under ==)
    assert json.dumps(r1, sort_keys=True, default=str) == json.dumps(
        r2, sort_keys=True, default=str)


def test_run_analysis_raises_without_folds():
    from config.loader import load_config
    cfg = load_config()
    df = _make_synthetic_wf_df(500, price=1.28, atr=0.0014, freq="1h")  # ~21 days
    with pytest.raises(ValueError, match="No walk-forward folds"):
        run_analysis(cfg, "GBPUSD", df)


def test_main_writes_csv_and_json(tmp_path, monkeypatch):
    """CLI smoke: no DB in the sandbox -> synthetic fallback path, files written.

    --allow-locked: the synthetic frame spans 2022-2026 and therefore overlaps
    the locked hold-out enabled on 2026-08-07. This test exercises the CLI
    plumbing (file writing), not the lock itself, so the lock is bypassed.
    """
    monkeypatch.chdir(tmp_path)
    from scripts.deflated_sharpe import main
    out = str(tmp_path / "dsr_gbp.csv")
    main(["--asset", "GBPUSD", "--variants", "current,null,v3_early_be,v4a",
          "--max-folds", "2", "--out", out, "--allow-locked"])
    assert os.path.exists(out)
    assert os.path.exists(out.replace(".csv", ".json"))
    # keep_default_na=False: the literal variant name "null" must not be
    # parsed into NaN by pandas' default NA list on the CSV round-trip.
    df = pd.read_csv(out, keep_default_na=False)
    assert list(df["variant"]) == ["current", "null", "v3_early_be", "v4a"]
    assert {"variant", "n_trades", "total_pnl", "sharpe", "psr_0",
            "dsr_trials", "dsr_historical", "min_trl_years"} <= set(df.columns)
    with open(out.replace(".csv", ".json"), encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["asset"] == "GBPUSD"
    assert payload["synthetic"] is True


# ---------------------------------------------------------------------------
# N_eff (dependent-trial correction) + CSCV audit extras
# ---------------------------------------------------------------------------


def test_effective_number_trials_perfectly_correlated():
    """Identical trials -> N_eff == 1 (one independent draw)."""
    from backtest.deflated_sharpe import effective_number_trials
    rng = np.random.default_rng(11)
    base = rng.normal(0.0, 1.0, 24)
    M = np.stack([base, base, base, base])  # rho = 1
    res = effective_number_trials(M)
    assert res["n_trials"] == 4
    assert res["n_eff"] == pytest.approx(1.0, abs=1e-6)


def test_effective_number_trials_uncorrelated():
    """Independent trials -> N_eff == M."""
    from backtest.deflated_sharpe import effective_number_trials
    rng = np.random.default_rng(12)
    M = rng.normal(0.0, 1.0, size=(5, 24))
    res = effective_number_trials(M)
    assert res["n_eff"] == pytest.approx(5.0, abs=0.5)


def test_effective_number_trials_audit_example():
    """Audit numbers: M=729, rho=0.95 -> N_eff ~= 37; rho=0.90 -> ~74."""
    from backtest.deflated_sharpe import effective_number_trials
    # Construct a matrix with approximate rho by mixing a common factor
    rng = np.random.default_rng(13)
    M = 729
    common = rng.normal(0.0, 1.0, 24)
    w = 0.9747  # ~ rho 0.95 after noise
    rows = [w * common + np.sqrt(1 - w ** 2) * rng.normal(0.0, 1.0, 24) for _ in range(M)]
    res = effective_number_trials(np.asarray(rows))
    n_eff_729 = 1.0 + (729 - 1.0) * (1.0 - res["mean_rho"])
    assert 25 < n_eff_729 < 55  # rho~0.95 -> ~37
    assert res["participation_ratio"] <= res["n_trials"]


def test_cscv_oos_prob_loss_and_degradation():
    """CSCV extras: dominant strategy -> OOS prob loss ~0; degradation reported."""
    rng = np.random.default_rng(14)
    M = rng.normal(0.0, 0.5, size=(5, 24))
    M[0, :] += 2.0
    res = cscv_pbo(M, random_seed=42)
    assert res["oos_prob_loss"] == 0.0
    # degradation is a finite relative number (on small blocks it can be either
    # sign by noise; the dominant trial must not be wiped out OOS)
    assert math.isfinite(res["is_oos_degradation"])
    assert res["is_oos_degradation"] > -0.5


def test_run_analysis_reports_n_eff(synthetic_gbp_df):
    from config.loader import load_config
    cfg = load_config()
    res = run_analysis(cfg, "GBPUSD", synthetic_gbp_df,
                       variants={"current": {}, "tight": {"signal_grid": {"stop_mult": 2.0}},
                                 "wide": {"signal_grid": {"stop_mult": 4.0}}, "null": None},
                       historical_trials=729)
    assert "n_eff" in res
    assert res["n_eff"]["n_eff_historical"] > 0
    assert res["n_eff"]["n_eff_historical"] <= 729
    assert res["cscv"]["oos_prob_loss"] is not None
    # every trial row carries the new DSR(N_eff) column
    for t in res["trials"]:
        assert "dsr_neff" in t
