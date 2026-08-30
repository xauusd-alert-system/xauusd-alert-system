"""
Tests for PSI feature-drift monitoring (scripts/monitor_feature_drift.py, TZ 5.3 / P2-40).

Run with: pytest scripts/tests/test_monitor_feature_drift.py -v
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.monitor_feature_drift import _psi_for_feature, check_drift


def _train_df(n: int = 2000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "feat_a": rng.normal(0.0, 1.0, n),
            "feat_b": rng.uniform(0.0, 1.0, n),
        }
    )


def test_psi_identical_zero():
    """Same distribution (literally the same data) -> PSI == 0 for all features."""
    df = _train_df()
    report = check_drift(df, df.copy())
    assert report["n_features_checked"] == 2
    for feat, psi in report["per_feature"].items():
        assert psi == pytest.approx(0.0, abs=1e-9)
    assert report["max_psi"] == pytest.approx(0.0, abs=1e-9)
    assert report["drifted_features"] == []


def test_psi_shifted_positive():
    """A strongly shifted live distribution -> psi > 0.2 and the feature is
    flagged as drifted."""
    train_df = _train_df()
    live_df = _train_df(seed=8) + 3.0  # big mean shift
    report = check_drift(train_df, live_df)
    assert report["per_feature"]["feat_a"] > 0.2
    assert "feat_a" in report["drifted_features"]
    assert report["max_psi"] > 0.2


def test_psi_handles_empty_bins():
    """Eps-protection: no division-by-zero / NaN when live bins are empty or
    the train distribution is degenerate (single unique value)."""
    rng = np.random.default_rng(3)
    train = pd.DataFrame({"f": rng.normal(0.0, 1.0, 500)})
    # Live sits entirely inside one train bin -> several train bins empty in live.
    live = pd.DataFrame({"f": rng.normal(0.0, 0.01, 200)})
    report = check_drift(train, live)
    psi = report["per_feature"]["f"]
    assert np.isfinite(psi)

    # Degenerate train distribution (constant value).
    train_const = pd.DataFrame({"f": np.full(100, 5.0)})
    live_const = pd.DataFrame({"f": np.full(100, 5.0)})
    assert _psi_for_feature(train_const["f"].to_numpy(float), live_const["f"].to_numpy(float)) == pytest.approx(
        0.0, abs=1e-9
    )

    # Constant train vs shifted live -> finite, large penalty (no crash/NaN).
    live_shifted = pd.DataFrame({"f": np.full(100, 9.0)})
    psi_shift = _psi_for_feature(train_const["f"].to_numpy(float), live_shifted["f"].to_numpy(float))
    assert np.isfinite(psi_shift)
    assert psi_shift > 0.2


def test_drift_report_structure():
    """The report exposes the documented keys with correct types."""
    train_df = _train_df()
    report = check_drift(train_df, _train_df(seed=99))
    assert set(report.keys()) == {
        "per_feature",
        "max_psi",
        "drifted_features",
        "drifted_psi_threshold",
        "n_features_checked",
    }
    assert isinstance(report["per_feature"], dict)
    assert isinstance(report["max_psi"], float)
    assert isinstance(report["drifted_features"], list)
    assert isinstance(report["drifted_psi_threshold"], float)
    assert report["drifted_psi_threshold"] == pytest.approx(0.2)
    assert report["n_features_checked"] == 2
    assert report["max_psi"] == max(report["per_feature"].values())


def test_cli_two_csvs(tmp_path, capsys):
    """CLI accepts two CSV files (tmp files) and prints/JSON-encodes a report."""
    from scripts.monitor_feature_drift import main as cli_main

    train_path = tmp_path / "train.csv"
    live_path = tmp_path / "live.csv"
    _train_df().to_csv(train_path, index=False)
    (_train_df(seed=11) + 3.0).to_csv(live_path, index=False)

    rc = cli_main(["--train", str(train_path), "--live", str(live_path), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["per_feature"]["feat_a"] > 0.2
    assert "feat_a" in report["drifted_features"]
