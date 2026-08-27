"""Numerical gradient checking (NN book ch. 3.10, pages 230-239; task T-11).

The book's engineering discipline: every self-implemented layer must pass a
central-difference gradient check before its gradients are trusted, because
a wrong backprop looks exactly like "poor convergence" and silently poisons
signals for months. Usage (also wired into CI as a pytest suite):

    from model.book_nn.gradient_check import gradient_check
    report = gradient_check(network, x, y)
    assert report["max_relative_error"] < 1e-5

For every parameter tensor a few random entries are perturbed by +/-eps and
the analytic gradient (network backward) is compared to the numerical one
via the maximum relative error over the sampled entries.
"""
from __future__ import annotations

import numpy as np

from model.book_nn.losses import get_loss


def numerical_gradient(loss_fn, param: np.ndarray, samples: int = 3,
                       eps: float = 1e-6) -> np.ndarray:
    """Central-difference gradient of a scalar loss for a few entries of
    ``param``. Returns an array of (index, numeric_grad) pairs."""
    flat = param.reshape(-1)
    rng = np.random.default_rng(7)
    n = min(samples, flat.size)
    if n == 0:
        return np.empty((0, 2))
    idx = rng.choice(flat.size, size=n, replace=False)
    out = []
    for i in idx:
        orig = flat[i]
        flat[i] = orig + eps
        loss_plus = loss_fn()
        flat[i] = orig - eps
        loss_minus = loss_fn()
        flat[i] = orig
        out.append((i, (loss_plus - loss_minus) / (2.0 * eps)))
    return np.array(out)


def gradient_check(network, x: np.ndarray, y: np.ndarray,
                   loss: str = "mse", eps: float = 1e-5,
                   samples_per_tensor: int = 3) -> dict:
    """Compare analytic vs numerical gradients for every parameter tensor.

    The per-entry error normalizes by max(|numeric|, |analytic|, 1e-3 x
    ||analytic||_inf over the sampled entries): entries whose gradient is
    genuinely ~0 would otherwise produce noise-driven relative errors, while
    a real backprop bug (wrong factor, missing term) lands at 1e-2..1.
    """
    loss_fn = get_loss(loss)
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)

    def compute_loss() -> float:
        pred = network.forward(x)
        return loss_fn(pred, y)[0]

    # analytic pass (gradients accumulate with +=, so zero first)
    network.zero_grad()
    pred = network.forward(x)
    _l, dpred = loss_fn(pred, y)
    network.backward(dpred)

    report: dict = {"tensors": {}, "max_relative_error": 0.0, "ok": True}
    for name, weight, grad in network.parameters():
        numeric = numerical_gradient(compute_loss, weight,
                                     samples=samples_per_tensor, eps=eps)
        pairs = [(int(idx), num, float(grad.reshape(-1)[int(idx)]))
                 for idx, num in numeric]
        scale = max([abs(a) for _i, _n, a in pairs] + [1e-12])
        errors = []
        for _i, num, ana in pairs:
            denom = max(abs(num), abs(ana), 1e-3 * scale)
            errors.append(abs(num - ana) / denom)
        max_err = float(max(errors)) if errors else 0.0
        report["tensors"][name] = {
            "max_relative_error": max_err,
            "entries_checked": len(errors),
        }
        report["max_relative_error"] = max(report["max_relative_error"], max_err)
    report["ok"] = report["max_relative_error"] < 1e-4
    return report


def assert_gradients_valid(network, x: np.ndarray, y: np.ndarray,
                           tolerance: float = 1e-4, **kwargs) -> dict:
    """Gradient check that raises AssertionError on failure (CI helper)."""
    report = gradient_check(network, x, y, **kwargs)
    if not report["ok"] or report["max_relative_error"] >= tolerance:
        worst = sorted(report["tensors"].items(),
                       key=lambda kv: -kv[1]["max_relative_error"])[:5]
        details = ", ".join(f"{k}={v['max_relative_error']:.2e}" for k, v in worst)
        raise AssertionError(
            f"gradient check failed: max relative error "
            f"{report['max_relative_error']:.3e} >= {tolerance:.0e} ({details})")
    return report
