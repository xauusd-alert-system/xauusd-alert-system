"""
Tests for Phase 7 Neural & Hybrid Trainer.
"""

import numpy as np
import pandas as pd
import pytest

from model.neural_trainer import (
    train_hybrid_model,
    train_neural_model,
)
from model.trainer import train_model


@pytest.fixture
def dummy_train_data():
    np.random.seed(42)
    n = 300
    X = pd.DataFrame(
        np.random.randn(n, 10),
        columns=[f"feat_{i}" for i in range(10)],
    )
    # Target correlated with features
    score = X["feat_0"] * 2.0 - X["feat_1"] + np.random.randn(n) * 0.5
    y = pd.Series((score > 0).astype(int))
    return X, y


def test_neural_classifier_fit_predict(dummy_train_data):
    X, y = dummy_train_data
    clf = train_neural_model(X, y)
    probs = clf.predict_proba(X)
    assert probs.shape == (len(X), 2)
    assert np.allclose(probs.sum(axis=1), 1.0)
    preds = clf.predict(X)
    assert len(preds) == len(X)
    assert set(np.unique(preds)).issubset({0, 1})


def test_hybrid_ensemble_model(dummy_train_data):
    X, y = dummy_train_data
    cfg = {"model": {"type": "xgboost", "random_seed": 42, "calibration_method": "sigmoid"}}
    tree_base = train_model(X, y, cfg)
    hybrid = train_hybrid_model(tree_base, X, y, cfg, tree_weight=0.6)
    probs = hybrid.predict_proba(X)
    assert probs.shape == (len(X), 2)
    assert np.allclose(probs.sum(axis=1), 1.0)
    preds = hybrid.predict(X)
    assert len(preds) == len(X)
