"""Adam optimizer (NN book ch. 1.4.3, formulas on pages 46-47).

    m_t = beta1 * m_{t-1} + (1 - beta1) * g_t
    v_t = beta2 * v_{t-1} + (1 - beta2) * g_t^2
    w_t = w_{t-1} - alpha * m_hat / (sqrt(v_hat) + eps)

with the book's defaults beta1=0.9, beta2=0.999 and the bias correction
(hat values) that accelerates the start of training. The optimizer works on
the (name, weight, grad) parameter tuples exposed by every layer, mirroring
the book's per-weight update code (`SGDUpdate`, `RMSPropUpdate`, ...).
"""
from __future__ import annotations

import numpy as np


class Adam:
    def __init__(self, lr: float = 1e-3, beta1: float = 0.9, beta2: float = 0.999,
                 eps: float = 1e-8):
        if lr <= 0:
            raise ValueError(f"lr must be positive, got {lr}")
        self.lr = float(lr)
        self.beta1 = float(beta1)
        self.beta2 = float(beta2)
        self.eps = float(eps)
        self.t = 0
        self._m: dict[int, np.ndarray] = {}
        self._v: dict[int, np.ndarray] = {}
        self._ids: dict[int, int] = {}
        self._next_id = 0

    def _slot(self, param: np.ndarray) -> int:
        pid = id(param)
        if pid not in self._ids:
            self._ids[pid] = self._next_id
            self._next_id += 1
            self._m[self._ids[pid]] = np.zeros_like(param)
            self._v[self._ids[pid]] = np.zeros_like(param)
        return self._ids[pid]

    def step(self, parameters: list) -> None:
        """parameters: list of (name, weight, grad) tuples; grads are consumed."""
        self.t += 1
        b1, b2 = self.beta1, self.beta2
        bias1 = 1.0 - b1 ** self.t
        bias2 = 1.0 - b2 ** self.t
        for _name, weight, grad in parameters:
            if grad is None:
                continue
            slot = self._slot(weight)
            m = self._m[slot]
            v = self._v[slot]
            m *= b1
            m += (1.0 - b1) * grad
            v *= b2
            v += (1.0 - b2) * (grad * grad)
            m_hat = m / bias1
            v_hat = v / bias2
            weight -= self.lr * m_hat / (np.sqrt(v_hat) + self.eps)

    def zero_grad(self, parameters: list) -> None:
        for _name, _weight, grad in parameters:
            if grad is not None:
                grad[...] = 0.0
