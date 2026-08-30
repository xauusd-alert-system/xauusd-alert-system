"""Regression tests for model.trainer.LogitMarginEstimator.

Production joblib bundles pickle this wrapper class by reference; it was
accidentally dropped from trainer.py (see logs/model_fingerprint_audit.csv:
"Can't get attribute 'LogitMarginEstimator'"), breaking ModelPredictor load.
These tests pin the restored contract: decision_function returns the log-odds
margin, predict_proba delegates, the object survives a pickle round-trip, and
the production bundle (if on disk) still loads.
"""

import pickle

import numpy as np
import pytest

from model.trainer import LogitMarginEstimator


class _StubBase:
    """Deterministic fake classifier: p = sigmoid(2*x)."""

    def __init__(self):
        self.classes_ = np.array([0, 1])

    def fit(self, X, y, **kwargs):  # pragma: no cover - trivial
        return self

    def predict_proba(self, X):
        x = np.asarray(X, dtype=float)
        p1 = 1.0 / (1.0 + np.exp(-2.0 * x))
        return np.column_stack([1 - p1, p1])


@pytest.fixture
def wrapped():
    return LogitMarginEstimator(_StubBase())


def test_decision_function_is_log_odds_margin(wrapped):
    X = np.array([[0.0], [10.0]])
    margins = wrapped.decision_function(X)
    # p at x=0 is 0.5 -> logit=0; x=10 saturates to the clip ceiling
    # (logit of p clipped at 1e-6 from both sides is ~13.8155).
    assert margins[0] == pytest.approx(0.0, abs=1e-9)
    assert margins[1] == pytest.approx(np.log((1 - 1e-6) / 1e-6), rel=1e-9)


def test_margin_is_monotonic_in_probability(wrapped):
    X = np.array([[-2.0], [0.0], [2.0]])
    m = wrapped.decision_function(X)
    assert m[0] < m[1] < m[2]


def test_predict_proba_delegates_and_sums_to_one(wrapped):
    proba = wrapped.predict_proba(np.array([[3.14]]))
    assert proba.shape == (1, 2)
    assert proba.sum() == pytest.approx(1.0)


def test_fit_sets_estimator_and_classes(wrapped):
    X = np.zeros((5, 1))
    y = np.array([0, 1, 0, 1, 0])
    out = wrapped.fit(X, y)
    assert out is wrapped
    assert wrapped.estimator_ is wrapped.estimator
    assert list(wrapped.classes_) == [0, 1]


def test_pickle_round_trip_preserves_class_identity(wrapped):
    # Mirrors the joblib unpickle path that failed in production.
    clone = pickle.loads(pickle.dumps(wrapped))
    assert type(clone) is LogitMarginEstimator
    np.testing.assert_allclose(
        clone.decision_function(np.array([[0.5]])),
        wrapped.decision_function(np.array([[0.5]])),
    )


def test_production_bundle_loads():
    import os

    import joblib

    path = os.environ.get("MODEL_PATH", "output/models/xauusd_direction_model.joblib")
    if not os.path.isfile(path):
        pytest.skip("production model not on disk")
    bundle = joblib.load(path)
    est = getattr(bundle["model"].calibrated_classifiers_[0], "estimator", None)
    # Old production bundles store the wrapper under 'estimator' (sklearn
    # clone convention); either way it must unpickle to the restored class.
    assert type(est).__name__ == "LogitMarginEstimator"
