"""TZ_BOOKS T-11: numerical gradient checks as an automated test.

The NN book (ch. 3.10, pages 230-239) makes the gradient check a REQUIRED
part of developing any new layer: analytic backprop is compared against
central finite differences on a small random problem. These tests pin
that contract for every stack the project trains, so a future edit to
``layers.py`` that breaks backprop fails CI instead of silently
poisoning training.

Tolerances follow the module's scaled-denominator convention (see
``model/book_nn/gradient_check.py``): eps=1e-5 for the softmax chains
(tighter eps drowns in finite-difference noise there - measured, not
assumed).
"""
from __future__ import annotations

import numpy as np
import pytest

from model.book_nn import (
    BookNetwork,
    assert_gradients_valid,
    book_fc_baseline_description,
    book_lstm_description,
    book_mha_description,
)


@pytest.fixture(scope="module")
def rng():
    return np.random.default_rng(20260828)


def _problem(network: BookNetwork, rng, n: int = 5):
    x = rng.normal(size=(n, network.input_window, network.input_dim))
    y = rng.normal(size=(n, network.output_dim))
    return x, y


def test_fc_stack_gradients(rng):
    net = BookNetwork(book_fc_baseline_description(hidden=8, output_dim=2),
                      input_window=4, input_dim=3, seed=1)
    x, y = _problem(net, rng)
    report = assert_gradients_valid(net, x, y)
    assert report["ok"]
    assert report["max_relative_error"] < 1e-6


def test_lstm_stack_gradients(rng):
    net = BookNetwork(book_lstm_description(hidden=6, output_dim=1),
                      input_window=5, input_dim=3, seed=2)
    x, y = _problem(net, rng)
    report = assert_gradients_valid(net, x, y)
    assert report["ok"]
    assert report["max_relative_error"] < 1e-5


def test_mha_stack_gradients(rng):
    net = BookNetwork(book_mha_description(heads=4, window_out=5, hidden=8,
                                           output_dim=1, model_dim=8),
                      input_window=5, input_dim=3, seed=3)
    x, y = _problem(net, rng)
    # softmax chains carry more finite-difference noise: the documented
    # tolerance for them is 1e-4 (see gradient_check.py header)
    report = assert_gradients_valid(net, x, y, tolerance=1e-4)
    assert report["ok"]
    assert report["max_relative_error"] < 1e-4


def test_gpt_stack_gradients(rng):
    """T-24 groundwork: the GPT-style block checks out too."""
    desc = [
        {"type": "gpt", "model_dim": 8, "step": 2, "ff_dim": 8},
        {"type": "fc", "count": 1, "activation": "linear"},
    ]
    net = BookNetwork(desc, input_window=4, input_dim=3, seed=4)
    x, y = _problem(net, rng)
    report = assert_gradients_valid(net, x, y, tolerance=1e-4)
    assert report["ok"]
    assert report["max_relative_error"] < 1e-4


def test_composite_stack_gradients(rng):
    """The book's composite: MHA encoder -> LSTM -> FC head."""
    desc = [
        {"type": "mha", "count": 4, "window_out": 4, "step": 2,
         "model_dim": 8, "activation": "linear"},
        {"type": "lstm", "count": 5, "activation": "linear"},
        {"type": "fc", "count": 1, "activation": "linear"},
    ]
    net = BookNetwork(desc, input_window=4, input_dim=3, seed=5)
    x, y = _problem(net, rng)
    report = assert_gradients_valid(net, x, y, tolerance=1e-4)
    assert report["ok"]
    assert report["max_relative_error"] < 1e-4


def test_gradient_check_detects_a_broken_backward():
    """The check itself must FAIL when backprop is wrong (guard the guard).

    Sabotage: the first FC layer accumulates dW scaled by 2 - exactly the
    class of bug the /scale fix in layers.py once carried.
    """
    net = BookNetwork(book_fc_baseline_description(hidden=8, output_dim=1),
                      input_window=3, input_dim=2, seed=6)
    rng = np.random.default_rng(6)
    x = rng.normal(size=(4, 3, 2))
    y = rng.normal(size=(4, net.output_dim))

    layer = net.layers[0]
    original_backward = layer.backward

    def broken_backward(grad_out):
        out = original_backward(grad_out)
        layer.dW *= 2.0          # classic wrong-factor backprop bug
        return out

    layer.backward = broken_backward

    from model.book_nn.gradient_check import gradient_check
    report = gradient_check(net, x, y)
    assert report["max_relative_error"] > 0.3, "sabotaged gradient passed!"
