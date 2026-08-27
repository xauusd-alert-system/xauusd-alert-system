"""Loss functions (NN book ch. 1.4.1, pages 28-33).

MSE (Gauss) - fast convergence on large errors, sensitive to outliers -
and MAE (Laplace) - linear across the whole error range. Both return
(loss, dLoss/dPrediction) so the training loop can chain straight into the
network backward pass. Book formulas:

    MAE = (1/n) * sum |y_i - y'_i|          (p. 28-29)
    MSE = (1/n) * sum (y_i - y'_i)^2        (p. 30)
"""
from __future__ import annotations

import numpy as np


def mse(pred: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray]:
    diff = np.asarray(pred, dtype=float) - np.asarray(target, dtype=float)
    n = diff.size
    if n == 0:
        return 0.0, np.zeros_like(diff)
    loss = float(np.mean(diff * diff))
    grad = 2.0 * diff / n
    return loss, grad


def mae(pred: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray]:
    diff = np.asarray(pred, dtype=float) - np.asarray(target, dtype=float)
    n = diff.size
    if n == 0:
        return 0.0, np.zeros_like(diff)
    loss = float(np.mean(np.abs(diff)))
    grad = np.sign(diff) / n
    return loss, grad


def get_loss(name: str):
    key = (name or "mse").lower()
    losses = {"mse": mse, "mae": mae}
    if key not in losses:
        raise ValueError(f"unknown loss {name!r}; known: {sorted(losses)}")
    return losses[key]
