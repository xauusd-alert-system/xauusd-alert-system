"""
The decision gate's fold condition (scripts/deflated_sharpe).

Pure-function tests: no SQLite, no model, no backtest engine. The fold shapes
are the real ones measured on XAUUSD M15 with `--end-date 2026-08-08`, so this
file doubles as a regression record of the run that exposed the old condition.

Why this file exists: on 2026-08-14 the honest harness reported 365 trades and
-396.5 for the shipped XAUUSD config, and the gate lit exactly one green box -
"positive folds >= 55% valid" at 4/7 = 57.1%. The condition counted votes and
ignored size, so one fold that lost -2293.4 over 152 trades weighed the same as
one that made +880.8 over six, and a one-trade fold voted at all.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.deflated_sharpe import (
    FOLD_CONDITION,
    MIN_TRADES_FOR_VALID_FOLD,
    PBO_CONDITION,
    PBO_MAX,
    POS_FOLD_SHARE_MIN,
    _summarize_trial,
    decision_gate,
    fold_health,
)

# (n_trades, fold PnL) per walk-forward fold, XAUUSD M15, --end-date 2026-08-08,
# as printed by scripts/run_backtest and reproduced fold-for-fold by
# scripts/deflated_sharpe after the harness parity fix. Total = -396.5.
XAUUSD_2026_08_14 = [
    (18, 256.5), (56, 91.7), (0, 0.0), (0, 0.0), (0, 0.0), (63, -327.5),
    (152, -2293.4), (0, 0.0), (1, -122.2), (6, 880.8), (0, 0.0), (69, 1117.6),
]


def _folds(spec: list[tuple[int, float]]) -> list[np.ndarray]:
    """Fold trade arrays with the requested trade count and total PnL.

    Trades inside a fold are spread around the mean so the fold contains both
    winners and losers (a constant array would have zero variance and make the
    Sharpe machinery meaningless).
    """
    out = []
    for n, pnl in spec:
        if n <= 0:
            out.append(np.array([], dtype=float))
            continue
        spread = np.linspace(-1.0, 1.0, n) * 50.0
        spread += (pnl - spread.sum()) / n
        out.append(spread.astype(float))
    return out


def _trial(spec: list[tuple[int, float]], name: str = "current") -> dict:
    return _summarize_trial(name, _folds(spec), n_folds=len(spec),
                            historical_trials=729, n_variants=5,
                            trades_per_year=140.0, n_eff_historical=341.64)


def _res(spec: list[tuple[int, float]], pbo: float = 0.324,
         slope: float = -0.79, trial: dict | None = None,
         cost_pf: float | None = None) -> dict:
    return {
        "trials": [trial if trial is not None else _trial(spec)],
        "cscv": {"pbo": pbo, "is_oos_slope": slope},
        "cost_stress": None if cost_pf is None else {"profit_factor": cost_pf},
    }


# ---------------------------------------------------------------------------
# The run that caused the change
# ---------------------------------------------------------------------------

def test_the_2026_08_14_xauusd_pattern_fails_the_fold_condition():
    """A -396.5 family must not pass a condition about its folds."""
    gate = decision_gate(_res(XAUUSD_2026_08_14))
    assert gate["checks"][FOLD_CONDITION] is False
    assert gate["passed_all"] is False


def test_the_vote_leg_still_passes_which_is_why_the_money_legs_exist():
    """The old condition, taken alone, STILL says yes on the losing family.

    3 of the 5 valid folds are positive (60%), comfortably over the 55%
    threshold. Nothing about the majority was wrong - it simply was not a
    result, which is why `total` and `ex-best` now sit beside it.
    """
    fh = fold_health(_trial(XAUUSD_2026_08_14))
    assert fh["valid_folds"] == 5
    assert fh["pos_folds"] == 3
    assert fh["positive_share"] == pytest.approx(0.6, abs=0.01)
    assert fh["positive_share_ok"] is True
    # ... and the two money legs are what reject it.
    assert fh["total_pnl"] == pytest.approx(-396.5, abs=0.5)
    assert fh["total_pnl_positive"] is False
    assert fh["total_pnl_ex_best"] == pytest.approx(-1514.1, abs=0.5)
    assert fh["ex_best_positive"] is False
    assert fh["passed"] is False


def test_the_dropped_median_leg_would_not_have_rejected_this_run():
    """Documents why "median valid fold > 0" is NOT one of the legs.

    The median of the valid folds is +91.7 here: positive, on a family that
    lost money. More generally the median cannot be negative while more than
    half of the folds are positive, so the leg could never fail once the 55%
    leg passed. It stays as a reported number only.
    """
    cur = _trial(XAUUSD_2026_08_14)
    assert cur["median_fold_pnl"] == pytest.approx(91.7, abs=0.5)
    assert cur["median_fold_pnl"] > 0
    assert fold_health(cur)["passed"] is False


# ---------------------------------------------------------------------------
# One leg at a time
# ---------------------------------------------------------------------------

def test_a_one_trade_fold_does_not_vote():
    """Fold 9 of the real run held a single trade (-122.24) and voted."""
    spec = [(20, 100.0), (20, 100.0), (20, 100.0), (1, -5000.0)]
    cur = _trial(spec)
    assert cur["traded_folds"] == 4
    assert cur["valid_folds"] == 3
    assert cur["pos_folds"] == 3
    fh = fold_health(cur)
    assert fh["positive_share"] == pytest.approx(1.0)
    # The excluded fold still costs real money, so the family is still rejected;
    # ignoring a fold's VOTE is not the same as ignoring its P&L.
    assert fh["total_pnl"] < 0
    assert fh["passed"] is False


def test_profit_that_lives_in_one_fold_fails_despite_a_majority_of_winners():
    """Only the concentration leg can reject this one.

    Four of five valid folds are positive and the total is +650, but deleting
    the single best window (+900) turns it into -250: one good period, not an
    edge.
    """
    spec = [(20, 50.0), (20, 50.0), (20, 50.0), (20, -400.0), (20, 900.0)]
    fh = fold_health(_trial(spec))
    assert fh["total_pnl_positive"] is True
    assert fh["positive_share_ok"] is True
    assert fh["best_fold_pnl"] == pytest.approx(900.0, abs=0.5)
    assert fh["total_pnl_ex_best"] == pytest.approx(-250.0, abs=0.5)
    assert fh["ex_best_positive"] is False
    assert fh["passed"] is False


def test_a_minority_of_positive_folds_fails_even_when_the_total_is_healthy():
    """Only the vote leg can reject this one: profitable, spread over two folds,
    but three of five valid folds lose."""
    spec = [(20, 900.0), (20, 800.0), (20, -100.0), (20, -100.0), (20, -100.0)]
    fh = fold_health(_trial(spec))
    assert fh["total_pnl_positive"] is True
    assert fh["ex_best_positive"] is True
    assert fh["pos_folds"] == 2
    assert fh["valid_folds"] == 5
    assert fh["positive_share_ok"] is False
    assert fh["passed"] is False


def test_a_broadly_profitable_family_passes_the_fold_condition():
    spec = [(20, 200.0)] * 5
    fh = fold_health(_trial(spec))
    assert (fh["total_pnl_positive"], fh["ex_best_positive"],
            fh["positive_share_ok"]) == (True, True, True)
    assert fh["passed"] is True
    assert decision_gate(_res(spec))["checks"][FOLD_CONDITION] is True


def test_folds_that_never_traded_enough_cannot_pass_anything():
    """Five folds of five trades each: profitable, but nothing votes."""
    spec = [(5, 100.0)] * 5
    cur = _trial(spec)
    assert cur["traded_folds"] == 5
    assert cur["valid_folds"] == 0
    fh = fold_health(cur)
    assert fh["total_pnl_positive"] is True
    assert fh["positive_share"] == 0.0
    assert fh["passed"] is False


def test_a_missing_current_variant_is_a_failure_not_a_pass():
    fh = fold_health(None)
    assert fh["passed"] is False
    gate = decision_gate({"trials": [_trial([(20, 100.0)] * 5, name="wide")],
                          "cscv": {"pbo": 0.05, "is_oos_slope": 0.9},
                          "cost_stress": {"profit_factor": 2.0}})
    assert gate["checks"][FOLD_CONDITION] is False
    assert gate["passed_all"] is False


# ---------------------------------------------------------------------------
# PBO band + thresholds
# ---------------------------------------------------------------------------

def test_pbo_inside_the_high_risk_band_no_longer_admits():
    """The report calls PBO in (0.20, 0.30] HIGH overfit risk, so the gate must
    not admit there. The measured value on 2026-08-14 was 0.324."""
    assert PBO_MAX == 0.20
    assert PBO_CONDITION == "PBO < 0.20"
    spec = [(20, 200.0)] * 5
    assert decision_gate(_res(spec, pbo=0.25))["checks"][PBO_CONDITION] is False
    assert decision_gate(_res(spec, pbo=0.324))["checks"][PBO_CONDITION] is False
    assert decision_gate(_res(spec, pbo=0.19))["checks"][PBO_CONDITION] is True


def test_thresholds_are_the_ones_the_report_prints():
    assert MIN_TRADES_FOR_VALID_FOLD == 10
    assert POS_FOLD_SHARE_MIN == 0.55
    assert FOLD_CONDITION == "folds: total PnL > 0, PnL ex-best fold > 0, 55% positive"


def test_the_gate_passes_only_when_every_measurable_condition_passes():
    """Sanity check in the other direction: the checklist is an AND, and the
    locked hold-out stays pending (None) instead of counting as a pass."""
    spec = [(20, 200.0)] * 5
    cur = _trial(spec)
    cur["t_block"] = 4.2
    cur["dsr_neff"] = 0.99
    res = _res(spec, pbo=0.05, slope=0.8, trial=cur, cost_pf=1.4)
    gate = decision_gate(res)
    assert gate["checks"]["locked hold-out confirms"] is None
    assert all(v for v in gate["checks"].values() if v is not None)
    assert gate["passed_all"] is True
    # Break one leg of the fold condition and the whole gate must close.
    cur_bad = dict(cur)
    cur_bad["total_pnl"] = -1.0
    gate_bad = decision_gate(_res(spec, pbo=0.05, slope=0.8,
                                  trial=cur_bad, cost_pf=1.4))
    assert gate_bad["checks"][FOLD_CONDITION] is False
    assert gate_bad["passed_all"] is False
