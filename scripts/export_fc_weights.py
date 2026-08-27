"""export_fc_weights - flatten a trained book FC model for the EA (T-18).

The EA's local-inference path (mql5/NeuroTrader/OpenCLInference.mqh +
NeuroTraderEA.mq5, EDGE mode) runs a two-layer MLP on the GPU/CPU:

    input (window * 7 normalized features)
        -> FC(hidden, Swish) -> FC(1, sigmoid) = probability

This script converts a model saved by ``model/book_nn/network.py`` (the
``.npz``/``.json`` pair written by ``run_book_experiments.py`` or any
other trainer) into the flat little-endian double file the EA loads:

    [ w1 (input*hidden, row-major) | b1 (hidden) | w2 (hidden) | b2 (1) ]

and a ``*_meta.json`` describing what was exported. Only two-layer FC
architectures (``fc swish -> fc linear``) can be exported - deeper or
recurrent stacks stay on the Python side of the bridge (BRIDGE mode).

Multi-output models (``multi_horizon`` target mode, output_dim=2) export
ONE head selected by ``--output-index`` (default 0 = the 6-bar horizon).
The sigmoid in the EA is applied to the linear output, so the exported
head must have been trained with a probability-compatible objective -
otherwise prefer BRIDGE mode, where Python does the inference.

Usage::

    python -m scripts.export_fc_weights --model /tmp/book_exp/book_fc \
        --out book_fc_weights.bin --output-index 0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logger = logging.getLogger("export_fc_weights")


def export(model_base: str, out_path: str, output_index: int = 0) -> dict:
    """Read ``<model_base>.json``/``.npz``; write the flat .bin + meta."""
    from model.book_nn import BookNetwork

    net = BookNetwork.load(model_base)
    desc = net.descriptions
    if len(desc) != 2 or desc[0]["type"] != "fc" or desc[1]["type"] != "fc":
        raise ValueError(
            f"only 2-layer FC stacks are exportable, got "
            f"{[d['type'] for d in desc]} - use BRIDGE mode for this model")
    if desc[0].get("activation") != "swish" or desc[1].get("activation",
                                                           "linear") != "linear":
        raise ValueError("expected fc(swish) -> fc(linear) architecture")

    with np.load(model_base + ".npz") as data:
        w1 = np.asarray(data["layer0_W"], dtype=np.float64)   # (in, hidden)
        b1 = np.asarray(data["layer0_b"], dtype=np.float64)   # (hidden,)
        w2 = np.asarray(data["layer1_W"], dtype=np.float64)   # (hidden, out)
        b2 = np.asarray(data["layer1_b"], dtype=np.float64)   # (out,)

    if not 0 <= output_index < w2.shape[1]:
        raise ValueError(f"output_index {output_index} outside 0..{w2.shape[1] - 1}")

    hidden = w1.shape[1]
    input_dim = w1.shape[0]
    flat = np.concatenate([
        w1.reshape(-1),                     # row-major (in, hidden)
        b1.reshape(-1),
        w2[:, output_index].reshape(-1),    # ONE output head
        b2[output_index:output_index + 1],
    ]).astype(np.float64)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(struct.pack(f"<{flat.size}d", *flat))

    meta = {
        "source_model": os.path.abspath(model_base),
        "source_config": os.path.abspath(model_base + ".json"),
        "output_index": output_index,
        "source_output_dim": int(w2.shape[1]),
        "input_dim": int(input_dim),
        "hidden_dim": int(hidden),
        "weight_count": int(flat.size),
        "dtype": "<f8 (little-endian float64)",
        "layout": "[w1(in*hidden) row-major | b1(hidden) | w2(hidden) | b2(1)]",
        "activation": "swish hidden, sigmoid output (applied by the EA)",
        "sha256": hashlib.sha256(
            struct.pack(f"<{flat.size}d", *flat)).hexdigest(),
        "ea_inputs": {
            "InpHiddenDim": int(hidden),
            "flattened_input_dim": int(input_dim),
            "note": "set the EA window so window * feature_count == "
                    "flattened_input_dim",
        },
    }
    meta_path = os.path.splitext(out_path)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    logger.info("exported %d weights (%d->%d->1, output head %d) -> %s",
                flat.size, input_dim, hidden, output_index, out_path)
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True,
                        help="model base path (without .npz/.json)")
    parser.add_argument("--out", default="book_fc_weights.bin",
                        help="output .bin for the EA (copy to MQL5\\Files)")
    parser.add_argument("--output-index", type=int, default=0,
                        help="which output head to export (multi-horizon)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    meta = export(args.model, args.out, args.output_index)
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
