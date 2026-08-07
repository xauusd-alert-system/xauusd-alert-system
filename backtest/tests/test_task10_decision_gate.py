import numpy as np
import pytest
from backtest.deflated_sharpe import decision_gate


def test_task10_decision_gate_rejects_when_pf_rises_but_t_falls():
    """Unit test Task 10: scenario where PF rises (1.07 -> 1.12) but block-bootstrap t
    does not increase (t_base = 1.30, t_filtered = 1.27) -> gate fails on 8th condition."""
    res = {
        "trials": [
            {
                "variant": "current",
                "t_block": 3.2,  # passes base condition 1 (>= 3.0)
                "dsr_neff": 0.96,  # passes condition 2 (> 0.95)
                "valid_folds": 20,
                "pos_folds": 14,  # 70% >= 55% (condition 5)
            }
        ],
        "cscv": {
            "pbo": 0.15,  # passes condition 3 (< 0.30)
            "is_oos_slope": 0.65,  # passes condition 6 (>= 0.5)
        },
        "cost_stress": {
            "profit_factor": 1.25,  # passes condition 4 (> 1.1)
        },
    }

    # Case A: Filtered t is lower than base t (e.g. t_base = 3.5, t_filtered = 3.2)
    gate_fail = decision_gate(res, t_base=3.5, t_filtered=3.2)
    assert gate_fail["checks"]["t_filtered > t_base (bootstrap t increased)"] is False
    assert gate_fail["passed_all"] is False

    # Case B: Filtered t equals base t (no increase)
    gate_fail_equal = decision_gate(res, t_base=3.2, t_filtered=3.2)
    assert gate_fail_equal["checks"]["t_filtered > t_base (bootstrap t increased)"] is False
    assert gate_fail_equal["passed_all"] is False

    # Case C: Filtered t is strictly higher (t_base = 3.0, t_filtered = 3.2) -> passes 8th condition
    gate_pass = decision_gate(res, t_base=3.0, t_filtered=3.2)
    assert gate_pass["checks"]["t_filtered > t_base (bootstrap t increased)"] is True


def test_task10_existing_7_conditions_still_functional():
    """Unit test Task 10: ensure all original 7 conditions are evaluated and intact."""
    res_failing_pbo = {
        "trials": [
            {
                "variant": "current",
                "t_block": 3.5,
                "dsr_neff": 0.98,
                "valid_folds": 20,
                "pos_folds": 15,
            }
        ],
        "cscv": {
            "pbo": 0.45,  # FAILS (< 0.30)
            "is_oos_slope": 0.70,
        },
        "cost_stress": {
            "profit_factor": 1.30,
        },
    }

    gate = decision_gate(res_failing_pbo)
    assert gate["checks"]["PBO < 0.30"] is False
    assert gate["passed_all"] is False
