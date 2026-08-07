import numpy as np
import pytest
from model.calibration import (
    compute_brier_score,
    compute_ece,
    calibration_report,
)


def test_task4_compute_brier_score():
    # Perfect predictions: Brier score = 0
    y_true = np.array([1, 0, 1, 0])
    y_prob = np.array([1.0, 0.0, 1.0, 0.0])
    assert compute_brier_score(y_true, y_prob) == 0.0

    # Uninformative 0.5 predictions: Brier score = (0.5)^2 = 0.25
    y_prob_uninformative = np.array([0.5, 0.5, 0.5, 0.5])
    assert compute_brier_score(y_true, y_prob_uninformative) == pytest.approx(0.25)


def test_task4_compute_ece_perfect_calibration():
    # Construct perfectly calibrated bins
    # In bin [0.8, 0.9], average prob 0.85, 85 out of 100 are positive
    y_prob = np.array([0.85] * 100 + [0.20] * 100)
    y_true = np.array([1] * 85 + [0] * 15 + [1] * 20 + [0] * 80)

    ece, diagram = compute_ece(y_true, y_prob, n_bins=10)
    assert ece == pytest.approx(0.0, abs=1e-3)
    assert len(diagram) == 10


def test_task4_calibration_report_gate_and_warning():
    # Poorly calibrated predictions: predicts 0.95 confidence, but actual win rate is only 0.50
    y_prob = np.array([0.95] * 100)
    y_true = np.array([1] * 50 + [0] * 50)

    rep = calibration_report(y_true, y_prob, n_bins=10, ece_threshold=0.05, asset_name="TEST_ASSET")

    assert rep["ece"] > 0.05
    assert rep["gate_passed"] is False
    assert rep["warning"] is not None
    assert "CALIBRATION GATE FAILED" in rep["warning"]
    assert len(rep["reliability_diagram"]) == 10
