"""
Phase 7: Neural Network and Hybrid Ensemble Trainer.
Provides multi-layer perceptron (MLP) classification with purged time-ordered
calibration and hybrid blending with tree-based models.

Status: @experimental (P2-36, TZ Часть 7 п.7.1).
NOT called from train_all_assets / run_backtest production pipelines —
kept as a research prototype with its own unit tests
(model/tests/test_neural_trainer.py). Do NOT wire into production without
a dedicated economic A/B and deploy_guard integration. Removal/reintegration
decision tracked in docs/TODO.md.
"""
from __future__ import annotations
import logging
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

from model.trainer import calibrate_model

logger = logging.getLogger("neural_trainer")


class NeuralSequenceClassifier:
    """
    Multi-layer neural network classifier with standard feature scaling
    and calibrated probability outputs.
    """

    def __init__(
        self,
        hidden_layer_sizes: tuple[int, ...] = (64, 32),
        activation: str = "relu",
        alpha: float = 0.001,
        max_iter: int = 200,
        random_state: int = 42,
    ):
        self.hidden_layer_sizes = hidden_layer_sizes
        self.activation = activation
        self.alpha = alpha
        self.max_iter = max_iter
        self.random_state = random_state
        self.pipeline: Optional[Pipeline] = None
        self.classes_: Optional[np.ndarray] = None

    def fit(self, X: pd.DataFrame | np.ndarray, y: pd.Series | np.ndarray) -> "NeuralSequenceClassifier":
        scaler = StandardScaler()
        mlp = MLPClassifier(
            hidden_layer_sizes=self.hidden_layer_sizes,
            activation=self.activation,
            alpha=self.alpha,
            max_iter=self.max_iter,
            random_state=self.random_state,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=10,
        )
        self.pipeline = Pipeline([
            ("scaler", scaler),
            ("mlp", mlp),
        ])
        self.pipeline.fit(X, y)
        self.classes_ = mlp.classes_
        return self

    def predict_proba(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        if self.pipeline is None:
            raise RuntimeError("Model is not fitted yet.")
        return self.pipeline.predict_proba(X)

    def predict(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        if self.pipeline is None:
            raise RuntimeError("Model is not fitted yet.")
        return self.pipeline.predict(X)


class HybridEnsembleModel:
    """
    Blends predictions of a gradient boosted tree model and a neural network model.
    """

    def __init__(self, tree_model, neural_model, tree_weight: float = 0.7):
        self.tree_model = tree_model
        self.neural_model = neural_model
        self.tree_weight = tree_weight
        self.neural_weight = 1.0 - tree_weight
        self.classes_ = getattr(tree_model, "classes_", np.array([0, 1]))

    def predict_proba(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        p_tree = self.tree_model.predict_proba(X)
        p_neural = self.neural_model.predict_proba(X)
        blended = self.tree_weight * p_tree + self.neural_weight * p_neural
        return blended

    def predict(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        proba = self.predict_proba(X)
        idx = np.argmax(proba, axis=1)
        return self.classes_[idx]


def train_neural_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    cfg: Optional[dict] = None,
) -> NeuralSequenceClassifier:
    """Train neural classifier on features."""
    seed = (cfg or {}).get("model", {}).get("random_seed", 42)
    model = NeuralSequenceClassifier(
        hidden_layer_sizes=(64, 32),
        max_iter=150,
        random_state=seed,
    )
    model.fit(X_train, y_train)
    return model


def train_hybrid_model(
    tree_base_model,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    cfg: Optional[dict] = None,
    tree_weight: float = 0.7,
) -> HybridEnsembleModel:
    """Train hybrid boosted tree + neural model."""
    neural = train_neural_model(X_train, y_train, cfg)
    hybrid = HybridEnsembleModel(tree_base_model, neural, tree_weight=tree_weight)
    return hybrid
