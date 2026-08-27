"""TZ_BOOKS T-17 + T-20: divergence monitoring and CLayerDescription configs."""
from __future__ import annotations

import json

import numpy as np
import pytest

from model.book_nn import BookNetwork, book_fc_baseline_description, fit
from model.book_nn.train import DivergenceConfig, TrainHistory


def _xy(seed: int, n: int = 128, window: int = 3, dim: int = 4):
    rng = np.random.default_rng(seed)
    return rng.normal(size=(n, window, dim)), rng.normal(size=(n, 1))


def test_config_serialization_roundtrip(tmp_path):
    """T-20: architectures are DATA (CLayerDescription), serialized to files."""
    net = BookNetwork(book_fc_baseline_description(hidden=12, output_dim=2),
                      input_window=3, input_dim=5, seed=1)
    base = str(tmp_path / "model")
    files = net.save(base)

    assert files["weights"].endswith(".npz")
    cfg = json.loads(open(files["config"], encoding="utf-8").read())
    assert cfg["input_window"] == 3
    assert cfg["input_dim"] == 5
    assert [d["type"] for d in cfg["layers"]] == ["fc", "fc"]

    restored = BookNetwork.load(base)
    assert restored.descriptions == net.descriptions
    assert restored.num_parameters() == net.num_parameters()
    x, y = _xy(2, dim=5)
    np.testing.assert_allclose(restored.forward(x), net.forward(x))


def test_weights_change_after_training():
    net = BookNetwork(book_fc_baseline_description(hidden=6, output_dim=1),
                      input_window=3, input_dim=4, seed=3)
    before = [w.copy() for _n, w, _g in net.parameters()]
    x, y = _xy(3)
    fit(net, x, y, epochs=3, batch_size=32, lr=1e-2, seed=3)
    after = [w for _n, w, _g in net.parameters()]
    assert any(not np.allclose(b, a) for b, a in zip(before, after))


def test_divergence_alarm_fires_on_overfitting():
    """T-17: train keeps improving, validation stalls -> alert recorded."""
    # train on one signal, validate on unrelated shifted noise
    rng = np.random.default_rng(4)
    x = rng.normal(size=(256, 3, 4))
    y = rng.normal(size=(256, 1))
    net = BookNetwork(book_fc_baseline_description(hidden=32, output_dim=1),
                      input_window=3, input_dim=4, seed=4)
    history = fit(net, x, y, X_val=x[:16], y_val=y[:16] + 5.0,
                  epochs=60, batch_size=64, lr=5e-3, seed=4,
                  divergence_cfg=DivergenceConfig(min_epochs=5, patience=3))
    assert isinstance(history, TrainHistory)
    assert history.divergence_detected
    assert any("divergence" in a for a in history.alerts)
    # the alert names the epoch and both curves
    alert = next(a for a in history.alerts if "divergence" in a)
    assert "epoch" in alert and "train" in alert


def test_no_divergence_on_healthy_run():
    rng = np.random.default_rng(5)
    x = rng.normal(size=(128, 3, 4))
    y = rng.normal(size=(128, 1))
    net = BookNetwork(book_fc_baseline_description(hidden=4, output_dim=1),
                      input_window=3, input_dim=4, seed=5)
    history = fit(net, x, y, X_val=x, y_val=y, epochs=3, batch_size=32,
                  lr=1e-3, seed=5)
    assert not history.divergence_detected


def test_history_rows_are_curve_export_ready():
    rng = np.random.default_rng(6)
    x, y = _xy(6)
    net = BookNetwork(book_fc_baseline_description(hidden=4, output_dim=1),
                      input_window=3, input_dim=4, seed=6)
    history = fit(net, x, y, epochs=2, batch_size=32, lr=1e-3, seed=6)
    rows = history.to_rows()
    assert len(rows) == 2
    for row in rows:
        assert {"epoch", "train_loss", "val_loss"} <= set(row)
