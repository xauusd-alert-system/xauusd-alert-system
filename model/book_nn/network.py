"""Declarative network construction + serialization (book ch. 3.4, task T-20).

Mirrors the book's ``CLayerDescription`` pattern: the architecture is DATA,
not code. A network is built from a list of layer descriptions such as

    [   # book p. 245-246: FC control point (60 neurons, Swish, linear out)
        {"type": "fc", "count": 60, "activation": "swish"},
        {"type": "fc", "count": 2,  "activation": "linear"},
    ]
    [   # book p. 512: MH Attention (8 heads, window_out=8)
        {"type": "mha", "count": 8, "window_out": 8, "step": 8,
         "activation": "linear"},
        {"type": "fc", "count": 60, "activation": "swish"},
        {"type": "fc", "count": 2,  "activation": "linear"},
    ]

Field names follow the book: ``type`` (layer kind), ``count`` (units /
output width), ``window`` (input sequence length, optional - inferred),
``window_out`` (output sequence length for attention layers), ``step``
(number of attention heads), ``activation``, ``optimization`` (lr/beta
hints consumed by the training loop). Serialization writes a JSON sidecar
(layer descriptions + shapes) and an NPZ weight file, so trained models are
files - never hard-coded architecture (task T-20).

Shape contract: ``forward`` takes (B, T, D) - a window of T feature vectors
per sample (or (B, D), treated as T=1). Sequence layers (lstm/mha/gpt)
consume/emit (B, T, D); FC layers always receive a flattened (B, features)
tensor - the network flattens 3D activations automatically and restores the
sequence shape during backward. The network output is always 2D (B, out).
"""
from __future__ import annotations

import json
import os

import numpy as np

from model.book_nn.layers import (
    FCLayer,
    GPTStyleBlock,
    LSTMLayer,
    MultiHeadAttentionLayer,
)

SERIALIZATION_VERSION = 1

_SEQUENCE_TYPES = ("lstm", "mha", "gpt")


def _normalize_description(desc: dict) -> dict:
    d = dict(desc)
    kind = str(d.get("type", "fc")).lower()
    aliases = {
        "dense": "fc", "neuron": "fc", "fully_connected": "fc", "fc": "fc",
        "rnn": "lstm", "lstm": "lstm",
        "attention": "mha", "mha": "mha", "multi_head_attention": "mha",
        "gpt": "gpt", "gpt_block": "gpt",
    }
    if kind not in aliases:
        raise ValueError(f"unknown layer type {d.get('type')!r}; "
                         f"known: {sorted(set(aliases.values()))}")
    d["type"] = aliases[kind]
    d.setdefault("activation", "linear")
    d.setdefault("count", None)
    d.setdefault("window_out", None)
    d.setdefault("step", None)
    d.setdefault("model_dim", None)
    d.setdefault("ff_dim", None)
    return d


class BookNetwork:
    """Sequential network built from CLayerDescription-style dicts."""

    def __init__(self, layer_descriptions: list[dict], input_window: int,
                 input_dim: int, seed: int = 42):
        if not layer_descriptions:
            raise ValueError("layer_descriptions must be non-empty")
        self.input_window = int(input_window)
        self.input_dim = int(input_dim)
        self.seed = int(seed)
        self.descriptions = [_normalize_description(d) for d in layer_descriptions]
        rng = np.random.default_rng(self.seed)
        self.layers: list = []
        self._kinds: list[str] = []
        self._flattened: list[bool] = []       # flatten applied before layer i
        self._final_flatten_shape: tuple | None = None

        cur_seq: int | None = self.input_window
        cur_dim = self.input_dim
        for desc in self.descriptions:
            kind = desc["type"]
            self._kinds.append(kind)
            self._flattened.append(False)
            if kind == "fc":
                if cur_seq is not None:        # 3D -> 2D transition
                    self._flattened[-1] = True
                    cur_dim = cur_seq * cur_dim
                    cur_seq = None
                out = int(desc["count"] or 1)
                self.layers.append(FCLayer(cur_dim, out, desc["activation"], rng=rng))
                cur_dim = out
            elif kind == "lstm":
                hidden = int(desc["count"] or 32)
                out = int(desc.get("out_dim") or desc["count"] or 1)
                self.layers.append(LSTMLayer(cur_dim, hidden, out,
                                             desc["activation"], rng=rng))
                cur_dim, cur_seq = out, None
            elif kind == "mha":
                heads = int(desc["step"] or 8)
                out_len = int(desc["window_out"] or 8)
                out_dim = int(desc["count"] or 8)
                model_dim = int(desc.get("model_dim") or 32)
                self.layers.append(MultiHeadAttentionLayer(
                    cur_dim, model_dim, heads, out_len, out_dim,
                    desc["activation"], causal=bool(desc.get("causal", False)),
                    rng=rng))
                cur_seq, cur_dim = out_len, out_dim
            elif kind == "gpt":
                model_dim = int(desc.get("model_dim") or desc.get("count") or 32)
                heads = int(desc["step"] or 8)
                ff_dim = int(desc.get("ff_dim") or 64)
                self.layers.append(GPTStyleBlock(cur_dim, model_dim, heads,
                                                 ff_dim, rng=rng))
                cur_dim = model_dim            # sequence length preserved
            else:  # pragma: no cover - guarded by _normalize_description
                raise ValueError(kind)
        if cur_seq is not None:                # trailing sequence layer
            self.output_dim = cur_seq * cur_dim
        else:
            self.output_dim = cur_dim
        self._in_shapes: list[tuple] = []

    # ------------------------------------------------------------------ API
    def forward(self, x: np.ndarray, training: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if x.ndim == 2:
            x = x[:, None, :]
        if x.ndim != 3:
            raise ValueError(f"expected (B, T, D) input, got shape {x.shape}")
        out = x
        self._in_shapes = []
        for i, layer in enumerate(self.layers):
            self._in_shapes.append(out.shape)
            if self._flattened[i]:
                out = out.reshape(out.shape[0], -1)
            out = layer.forward(out, training=training)
        if out.ndim == 3:
            self._final_flatten_shape = out.shape
            out = out.reshape(out.shape[0], -1)
        else:
            self._final_flatten_shape = None
        return out

    def backward(self, grad_out: np.ndarray) -> np.ndarray:
        if self._final_flatten_shape is not None:
            grad_out = np.asarray(grad_out, dtype=float).reshape(self._final_flatten_shape)
        grad = np.asarray(grad_out, dtype=float)
        for i in range(len(self.layers) - 1, -1, -1):
            grad = self.layers[i].backward(grad)
            if self._flattened[i]:
                grad = grad.reshape(self._in_shapes[i])
        return grad

    def parameters(self) -> list:
        params = []
        for layer in self.layers:
            params.extend(layer.parameters())
        return params

    def num_parameters(self) -> int:
        return int(sum(w.size for _n, w, _g in self.parameters()))

    def zero_grad(self) -> None:
        for _n, _w, g in self.parameters():
            if g is not None:
                g[...] = 0.0

    def layer_kinds(self) -> list[str]:
        return list(self._kinds)

    # -------------------------------------------------------- serialization
    def to_config(self) -> dict:
        return {
            "serialization_version": SERIALIZATION_VERSION,
            "input_window": self.input_window,
            "input_dim": self.input_dim,
            "seed": self.seed,
            "output_dim": self.output_dim,
            "layers": self.descriptions,
        }

    def save(self, base_path: str) -> dict:
        """Write ``<base>.json`` (architecture) + ``<base>.npz`` (weights)."""
        directory = os.path.dirname(os.path.abspath(base_path))
        os.makedirs(directory, exist_ok=True)
        arrays = {}
        for li, layer in enumerate(self.layers):
            for name, weight, _grad in layer.parameters():
                arrays[f"layer{li}_{name}"] = weight
        np.savez(base_path + ".npz", **arrays)
        with open(base_path + ".json", "w", encoding="utf-8") as fh:
            json.dump(self.to_config(), fh, indent=2)
        return {"weights": base_path + ".npz", "config": base_path + ".json"}

    @classmethod
    def load(cls, base_path: str) -> "BookNetwork":
        with open(base_path + ".json", "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if cfg.get("serialization_version", 1) > SERIALIZATION_VERSION:
            raise ValueError("model serialized by a newer library version")
        net = cls(cfg["layers"], cfg["input_window"], cfg["input_dim"],
                  seed=cfg.get("seed", 42))
        with np.load(base_path + ".npz") as data:
            for li, layer in enumerate(net.layers):
                for name, weight, _grad in layer.parameters():
                    key = f"layer{li}_{name}"
                    if key not in data:
                        raise KeyError(f"missing weight {key} in {base_path}.npz")
                    weight[...] = data[key]
        return net


def book_fc_baseline_description(hidden: int = 60, output_dim: int = 2) -> list[dict]:
    """Task T-04 control point: 60 neurons, Swish, Adam, Linear output
    (NN book pages 245-246, 254)."""
    return [
        {"type": "fc", "count": hidden, "activation": "swish",
         "optimization": {"method": "adam"}},
        {"type": "fc", "count": output_dim, "activation": "linear"},
    ]


def book_mha_description(heads: int = 8, window_out: int = 8, hidden: int = 60,
                         output_dim: int = 2, model_dim: int = 32) -> list[dict]:
    """Task T-09: MH Attention, 8 heads, window_out=8, Adam (book p. 512-513)."""
    return [
        {"type": "mha", "count": 8, "window_out": window_out, "step": heads,
         "model_dim": model_dim, "activation": "linear",
         "optimization": {"method": "adam"}},
        {"type": "fc", "count": hidden, "activation": "swish"},
        {"type": "fc", "count": output_dim, "activation": "linear"},
    ]


def book_lstm_description(hidden: int = 32, output_dim: int = 2) -> list[dict]:
    """LSTM comparison model (book ch. 4.2)."""
    return [
        {"type": "lstm", "count": hidden, "activation": "linear",
         "optimization": {"method": "adam"}},
        {"type": "fc", "count": output_dim, "activation": "linear"},
    ]
