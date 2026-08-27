"""TZ_BOOKS T-25 + the artifact scripts: ensemble voting, initial data,
FC weight export."""
from __future__ import annotations

import json
import struct

import numpy as np
import pandas as pd
import pytest

from model.book_nn import BookNetwork, book_fc_baseline_description
from model.book_nn.ensemble_vote import (
    ensemble_vote,
    model_probability,
    signal_stability,
)
from scripts.create_initial_data_xauusd import (
    build_day_filter_config,
    main as create_initial_data,
)
from scripts.export_fc_weights import export as export_weights


# --------------------------------------------------------------------- T-25
def test_model_probability_one_and_two_column():
    one = model_probability(np.array([0.0, 2.0, -2.0]))
    assert one.shape == (3,)
    assert one[0] == pytest.approx(0.5)
    assert one[1] > 0.8 and one[2] < 0.2

    two = model_probability(np.array([[1.0, 0.0], [0.0, 1.0]]))
    assert two[0] > 0.7 and two[1] < 0.3
    assert two[0] + two[1] == pytest.approx(1.0)  # only for symmetric rows


def test_ensemble_vote_requires_agreement():
    # all three models confidently long -> signal long
    probs = {"fc": np.array([0.9]), "lstm": np.array([0.85]),
             "mha": np.array([0.88])}
    out = ensemble_vote(probs, trade_level=0.6, min_agreement=0.6)
    s = out["samples"][0]
    assert s["signal"] == "long"
    assert s["agreement"] == 1.0

    # one dissenter still passes 2/3 >= 0.6
    probs = {"fc": np.array([0.9]), "lstm": np.array([0.85]),
             "mha": np.array([0.2])}
    out = ensemble_vote(probs, trade_level=0.6, min_agreement=0.6)
    assert out["samples"][0]["signal"] == "long"
    assert out["samples"][0]["agreement"] == pytest.approx(2 / 3)

    # two dissenters: unstable -> flat even though the mean clears 0.6
    probs = {"fc": np.array([0.9]), "lstm": np.array([0.3]),
             "mha": np.array([0.35])}
    out = ensemble_vote(probs, trade_level=0.6, min_agreement=0.6)
    assert out["samples"][0]["signal"] == "flat"


def test_ensemble_vote_short_side():
    probs = {"fc": np.array([0.05]), "lstm": np.array([0.08]),
             "mha": np.array([0.02])}
    out = ensemble_vote(probs, trade_level=0.6, min_agreement=0.6)
    s = out["samples"][0]
    assert s["signal"] == "short"
    assert s["model_votes"] == {"fc": -1, "lstm": -1, "mha": -1}


def test_ensemble_vote_flat_below_threshold():
    probs = {"fc": np.array([0.55]), "lstm": np.array([0.54]),
             "mha": np.array([0.56])}
    out = ensemble_vote(probs, trade_level=0.6)
    assert out["samples"][0]["signal"] == "flat"


def test_signal_stability_bounds():
    probs = {"fc": np.array([0.9, 0.5]), "lstm": np.array([0.9, 0.5]),
             "mha": np.array([0.9, 0.4])}
    stability = signal_stability(probs, trade_level=0.6)
    assert 0.0 <= stability <= 1.0
    # first sample: full agreement; second: no direction -> 0
    assert stability == pytest.approx(0.5)


# ------------------------------------------------------- create_initial_data
def test_create_initial_data_synthetic_run(tmp_path, monkeypatch):
    out_dir = str(tmp_path / "initial")
    monkeypatch.chdir(tmp_path)
    code = create_initial_data([
        "--synthetic", "--bars", "1200", "--out-dir", out_dir])
    assert code == 0

    for name in ("samples.npz", "normalization_params.json",
                 "book_normalization.json", "book_day_filter.json",
                 "dataset_meta.json"):
        assert (tmp_path / "initial" / name).exists(), name

    with open(f"{out_dir}/dataset_meta.json", encoding="utf-8") as fh:
        meta = json.load(fh)
    assert meta["synthetic"] is True
    assert meta["split"]["train"] > meta["split"]["valid"] > 0

    # the EA copy is the same normalization contract
    with open(f"{out_dir}/book_normalization.json", encoding="utf-8") as fh:
        ea_norm = json.load(fh)
    assert ea_norm["columns"] == meta["feature_columns"]
    assert set(ea_norm["center"]) == set(ea_norm["scale"])
    assert all(v > 0 for v in ea_norm["scale"].values())

    # day filter is fail-open without trade statistics
    with open(f"{out_dir}/book_day_filter.json", encoding="utf-8") as fh:
        day_cfg = json.load(fh)
    assert day_cfg["enabled"] is False
    assert day_cfg["days_blocked"] == []


def test_day_filter_config_from_trades_csv(tmp_path):
    # 40 catastrophic Monday trades -> Monday gets blocked
    rows = []
    for i in range(40):
        ts = pd.Timestamp("2024-01-01") + pd.Timedelta(weeks=i, hours=3)
        rows.append({"time": ts, "pnl": -10.0 if i % 3 else 5.0})
    csv_path = tmp_path / "trades.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    cfg = build_day_filter_config(str(csv_path))
    assert cfg["enabled"] is True
    assert cfg["days_blocked"] == [0]


# --------------------------------------------------------- export_fc_weights
def _tiny_trained_fc(tmp_path, seed: int = 5) -> str:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(200, 3, 4))
    y = rng.normal(size=(200, 2))
    net = BookNetwork(book_fc_baseline_description(hidden=6, output_dim=2),
                      input_window=3, input_dim=4, seed=seed)
    from model.book_nn import fit
    fit(net, X, y, epochs=2, batch_size=32, lr=1e-3, seed=seed)
    base = str(tmp_path / "tiny_fc")
    net.save(base)
    return base


def test_export_fc_weights_layout_and_math(tmp_path):
    base = _tiny_trained_fc(tmp_path)
    out_bin = str(tmp_path / "weights.bin")
    meta = export_weights(base, out_bin, output_index=0)

    assert meta["input_dim"] == 3 * 4      # flattened window
    assert meta["hidden_dim"] == 6
    assert meta["weight_count"] == 3 * 4 * 6 + 6 + 6 + 1

    with open(out_bin, "rb") as fh:
        flat = np.array(struct.unpack(f"<{meta['weight_count']}d",
                                      fh.read()))
    w1 = flat[:12 * 6].reshape(12, 6)
    b1 = flat[12 * 6:12 * 6 + 6]
    w2 = flat[12 * 6 + 6:12 * 6 + 12]
    b2 = flat[-1]

    # the exported head must reproduce sigmoid(net(x)) for the chosen column
    net = BookNetwork.load(base)
    x = np.random.default_rng(9).normal(size=(5, 3, 4))
    net_out = net.forward(x)                     # linear, 2 columns
    xf = x.reshape(5, -1)
    hidden = xf @ w1 + b1
    swish = hidden / (1.0 + np.exp(-hidden))
    manual = 1.0 / (1.0 + np.exp(-(swish @ w2 + b2)))
    expected = 1.0 / (1.0 + np.exp(-net_out[:, 0]))
    assert np.allclose(manual, expected, atol=1e-12)


def test_export_fc_weights_rejects_other_architectures(tmp_path):
    from model.book_nn import book_lstm_description
    net = BookNetwork(book_lstm_description(hidden=4, output_dim=1),
                      input_window=3, input_dim=4, seed=1)
    base = str(tmp_path / "lstm_model")
    net.save(base)
    with pytest.raises(ValueError):
        export_weights(base, str(tmp_path / "bad.bin"))
