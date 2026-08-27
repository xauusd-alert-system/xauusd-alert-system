"""
Tests for calibration monitoring (scripts/monitor_calibration.py, TZ 5.3 / P2-46).

Run with: pytest scripts/tests/test_monitor_calibration.py -v
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.monitor_calibration import check_calibration


def test_brier_perfect_zero():
    """Perfect predictions with full confidence -> Brier score 0."""
    preds = [1.0, 0.0, 1.0, 0.0]
    outs = [1, 0, 1, 0]
    report = check_calibration(preds, outs)
    assert report["brier_score"] == pytest.approx(0.0, abs=1e-12)
    assert report["ece"] == pytest.approx(0.0, abs=1e-12)


def test_brier_known_value():
    """Hand-computed Brier score: mean((pred - outcome)^2).

    (0.8-1)^2=0.04, (0.4-0)^2=0.16, (0.6-1)^2=0.16 -> mean = 0.12
    """
    preds = [0.8, 0.4, 0.6]
    outs = [1, 0, 1]
    report = check_calibration(preds, outs)
    assert report["brier_score"] == pytest.approx(0.12, abs=1e-12)


def test_ece_perfect_zero():
    """Perfectly calibrated predictions (per-bin mean_pred == mean_outcome)
    -> ECE 0."""
    # Bin [0.2,0.3): pred 0.25 x4 with 1/4 positives -> mean_pred == mean_outcome.
    # Bin [0.7,0.8): pred 0.75 x4 with 3/4 positives -> identical equality.
    preds = [0.25, 0.25, 0.25, 0.25, 0.75, 0.75, 0.75, 0.75]
    outs = [0, 0, 0, 1, 1, 1, 0, 1]
    report = check_calibration(preds, outs)
    assert report["ece"] == pytest.approx(0.0, abs=1e-12)
    assert report["is_calibrated"] is True


def test_ece_miscalibrated_positive():
    """Confidently wrong predictions -> ECE > 0.1 threshold."""
    # All predictions 0.9, all outcomes 0 -> bin mean_pred=0.9, mean_outcome=0
    # -> ECE = 0.9.
    preds = [0.9] * 10
    outs = [0] * 10
    report = check_calibration(preds, outs)
    assert report["ece"] == pytest.approx(0.9, abs=1e-9)
    assert report["ece"] > 0.1
    assert report["is_calibrated"] is False
    # Reliability data present for the single populated bin.
    assert len(report["reliability"]) == 1
    assert report["reliability"][0]["n"] == 10
    assert report["reliability"][0]["mean_pred"] == pytest.approx(0.9)
    assert report["reliability"][0]["mean_outcome"] == pytest.approx(0.0)


def test_cli_jsonl_input(tmp_path, capsys):
    """CLI accepts a jsonl file with pred/outcome columns (tmp file)."""
    from scripts.monitor_calibration import main as cli_main

    path = tmp_path / "preds.jsonl"
    records = [{"pred": p, "outcome": o} for p, o in zip([0.9] * 8, [0] * 8)]
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    rc = cli_main(["--input", str(path), "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["n_predictions"] == 8
    assert report["ece"] > 0.1
    assert report["is_calibrated"] is False
