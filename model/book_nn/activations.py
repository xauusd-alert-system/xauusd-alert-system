"""Activation functions of the book NN library (NN book ch. 1.2).

Each activation is exposed as a pair (forward, derivative-of-z) following the
book's pattern `double Act*(double x)` on MQL5 / `def Act*(x)` on Python
(NN book pages 12-22, 245). The derivative is expressed through the
pre-activation value z so backprop never re-runs the forward transform.

Implemented: step (not differentiable - kept for parity with the book),
linear a*x+b (book default a=1, b=0), sigmoid (+ modified sigmoid a/(1+e^-x)-b),
tanh, relu, lrelu, elu, swish (used in the book's chapter-3 experiments,
p. 245).
"""
from __future__ import annotations

from typing import Callable

import numpy as np


def _step(z, theta: float = 0.0):
    return (z >= theta).astype(float)


def _step_deriv(z):  # a.e. zero derivative - documented non-trainable (book p. 13)
    return np.zeros_like(z)


def make_linear(a: float = 1.0, b: float = 0.0):
    def f(z):
        return a * z + b

    def d(z):
        return np.full_like(np.asarray(z, dtype=float), a)

    return f, d


def sigmoid(z):
    z = np.asarray(z, dtype=float)
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def sigmoid_deriv(z):
    s = sigmoid(z)
    return s * (1.0 - s)  # book p. 15: f'(x) = f(x)(1 - f(x))


def make_modified_sigmoid(a: float = 1.0, b: float = 0.0):
    """Book p. 15: f(x) = a / (1 + e^-x) - b."""
    def f(z):
        return a * sigmoid(z) - b

    def d(z):
        return a * sigmoid(z) * (1.0 - sigmoid(z))

    return f, d


def tanh(z):
    return np.tanh(z)


def tanh_deriv(z):
    t = np.tanh(z)
    return 1.0 - t * t


def relu(z):
    return np.maximum(z, 0.0)


def relu_deriv(z):
    return (z > 0).astype(float)


def make_lrelu(alpha: float = 0.01):
    def f(z):
        return np.where(z >= 0, z, alpha * z)

    def d(z):
        return np.where(z >= 0, 1.0, alpha)

    return f, d


def make_elu(alpha: float = 1.0):
    def f(z):
        return np.where(z >= 0, z, alpha * (np.exp(np.minimum(z, 0.0)) - 1.0))

    def d(z):
        return np.where(z >= 0, 1.0, alpha * np.exp(np.minimum(z, 0.0)))

    return f, d


def swish(z):
    """Swish / SiLU: z * sigmoid(z). Used by the book's chapter-3 experiments."""
    return z * sigmoid(z)


def swish_deriv(z):
    s = sigmoid(z)
    return s * (1.0 + z * (1.0 - s))


_ACTIVATIONS: dict[str, tuple[Callable, Callable]] = {
    "step": (_step, _step_deriv),
    "linear": make_linear(1.0, 0.0),
    "sigmoid": (sigmoid, sigmoid_deriv),
    "tanh": (tanh, tanh_deriv),
    "relu": (relu, relu_deriv),
    "lrelu": make_lrelu(0.01),
    "elu": make_elu(1.0),
    "swish": (swish, swish_deriv),
}


def get_activation(name: str) -> tuple[Callable, Callable]:
    """Resolve an activation by book name; `AF_*` prefixes are tolerated."""
    key = (name or "linear").lower()
    if key.startswith("af_"):
        key = key[3:]
    if key not in _ACTIVATIONS:
        raise ValueError(f"unknown activation {name!r}; known: {sorted(_ACTIVATIONS)}")
    return _ACTIVATIONS[key]


def available_activations() -> list[str]:
    return sorted(_ACTIVATIONS)
