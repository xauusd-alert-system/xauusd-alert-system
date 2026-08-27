"""Tests for scripts.verify_model_fingerprints — fingerprint integrity plus the
2026-08-27 probability-degeneracy audit (std p < min_std or constant output),
which the overnight pipeline runs fail-closed so a collapsed model cannot stay
deployed silently (GBPUSD 0.0002 / XAGUSD 0.475371 constant class).
"""

import os
import tempfile

import joblib
import numpy as np
import pandas as pd
import pytest

from model.trainer import compute_model_fingerprint
from scripts.verify_model_fingerprints import (
    degeneracy_stats,
    _is_degenerate,
    verify_file,
)


# ---------------------------------------------------------------------------
# Degeneracy helpers
# ---------------------------------------------------------------------------

class _FakeBinaryModel:
    """Picklable fake binary classifier: classes_ = [0, 1] (short, long).

    ``variation`` scales the per-row deviation so tests can choose between a
    wide-spread model (std clearly above 0.01) and a collapsed/constant one.
    """

    def __init__(self, p_long, p_short, variation: float = 1e-3):
        self.p_long = np.asarray(p_long, dtype=float)
        self.p_short = np.asarray(p_short, dtype=float)
        self.variation = variation
        self.classes_ = np.array([0, 1])

    def predict_proba(self, X):
        n = len(X)
        pl = np.full(n, float(np.mean(self.p_long)))
        ps = np.full(n, float(np.mean(self.p_short)))
        pl = pl + self.p_long[:n] * self.variation
        ps = ps + self.p_short[:n] * self.variation
        return np.column_stack([ps, pl])

    def __repr__(self):
        # Deterministic repr (no memory address): the content fingerprint uses
        # repr as the non-XGBoost fallback, and it must be stable across a
        # pickle round-trip for the self-hash to verify.
        return (f"_FakeBinaryModel(variation={self.variation!r}, "
                f"p_long_mean={float(np.mean(self.p_long))!r}, "
                f"p_short_mean={float(np.mean(self.p_short))!r})")


def _binary_model(p_long: np.ndarray, p_short: np.ndarray,
                  variation: float = 1e-3):
    """Wrap two probability vectors in a fake fitted binary model."""
    return _FakeBinaryModel(p_long, p_short, variation)


def test_degeneracy_stats_spread_model():
    model = _binary_model(np.linspace(0.4, 0.6, 50), np.linspace(0.6, 0.4, 50),
                          variation=0.5)
    X = pd.DataFrame({"f": np.zeros(50)})
    stats = degeneracy_stats(model, ["f"], X)
    assert stats["n"] == 50
    assert stats["std_p_long"] > 0.01
    assert stats["std_p_short"] > 0.01
    degen, why = _is_degenerate(stats, min_std=0.01)
    assert not degen


def test_degeneracy_stats_constant_output():
    model = _binary_model(np.full(50, 0.475371), np.full(50, 0.524629),
                          variation=0.0)
    X = pd.DataFrame({"f": np.zeros(50)})
    stats = degeneracy_stats(model, ["f"], X)
    assert stats["nunique_p_long"] == 1
    degen, why = _is_degenerate(stats, min_std=0.01)
    assert degen
    assert "constant" in why


def test_is_degenerate_narrow_band():
    # Collapsed band: std below the 0.01 floor (the GBPUSD/XAGUSD class).
    stats = {"n": 100, "std_p_long": 0.0002, "std_p_short": 0.0002,
             "nunique_p_long": 40, "nunique_p_short": 40}
    degen, why = _is_degenerate(stats, min_std=0.01)
    assert degen
    assert "std_p" in why


def test_is_degenerate_no_probe_data():
    assert not _is_degenerate(None, 0.01)[0]
    assert not _is_degenerate({"error": "boom"}, 0.01)[0]
    assert not _is_degenerate({"n": 0}, 0.01)[0]


# ---------------------------------------------------------------------------
# verify_file verdicts
# ---------------------------------------------------------------------------

def _make_bundle(model, feature_cols, metadata=None):
    return {"model": model, "feature_cols": feature_cols,
            "metadata": metadata or {}}


def _dump_with_self_hash(tmp_path, name, model, cols, metadata=None):
    """Dump a bundle whose stored model_hash is the fingerprint of the RELOADED
    object (pickle round-trip changes repr for fake models, so the hash must be
    computed from the post-load object to be honest — exactly what the audit
    checks).
    """
    path = str(tmp_path / name)
    joblib.dump(_make_bundle(model, cols, metadata or {}), path)
    loaded = joblib.load(path)
    fp = compute_model_fingerprint(loaded["model"], cols)
    joblib.dump(_make_bundle(loaded["model"], cols, {**(metadata or {}), "model_hash": fp}), path)
    return path


def test_verify_file_degenerate_beats_new_ok(tmp_path):
    model = _binary_model(np.full(60, 0.475), np.full(60, 0.525),
                          variation=0.0)
    path = _dump_with_self_hash(tmp_path, "xagusd_direction_model.joblib",
                                model, ["f"])
    X = pd.DataFrame({"f": np.zeros(60)})
    row = verify_file(path, probe_X=X, min_std=0.01, asset_key="XAGUSD")
    assert row["verdict"] == "DEGENERATE"
    assert row["degenerate"] is True
    assert row["std_p_long"] is not None


def test_verify_file_healthy_new_ok(tmp_path):
    model = _binary_model(np.linspace(0.30, 0.70, 80), np.linspace(0.70, 0.30, 80),
                          variation=0.5)
    path = _dump_with_self_hash(tmp_path, "btcusd_direction_model.joblib",
                                model, ["f"])
    X = pd.DataFrame({"f": np.zeros(80)})
    row = verify_file(path, probe_X=X, min_std=0.01, asset_key="BTCUSD")
    assert row["verdict"] == "NEW-OK"
    assert row["degenerate"] is False


def test_verify_file_mismatch_wins_over_degenerate(tmp_path):
    model = _binary_model(np.full(60, 0.475), np.full(60, 0.525),
                          variation=0.0)
    cols = ["f"]
    bundle = _make_bundle(model, cols, {"model_hash": "0" * 64})  # wrong hash
    path = str(tmp_path / "gbpusd_direction_model.joblib")
    joblib.dump(bundle, path)
    X = pd.DataFrame({"f": np.zeros(60)})
    row = verify_file(path, probe_X=X, min_std=0.01, asset_key="GBPUSD")
    # Both broken: hash mismatch takes priority (integrity > spread).
    assert row["verdict"] == "NEW-MISMATCH"


def test_verify_file_unrecognized_load_failure(tmp_path):
    path = str(tmp_path / "broken.joblib")
    with open(path, "wb") as f:
        f.write(b"this is not a joblib file at all")
    row = verify_file(path)
    assert row["verdict"] == "UNRECOGNIZED"
    assert "load failed" in row["note"]


def test_verify_file_legacy_without_hash(tmp_path):
    model = _binary_model(np.linspace(0.3, 0.7, 40), np.linspace(0.7, 0.3, 40))
    bundle = _make_bundle(model, ["f"])
    path = str(tmp_path / "archive.joblib")
    joblib.dump(bundle, path)
    row = verify_file(path)
    assert row["verdict"] == "LEGACY"
