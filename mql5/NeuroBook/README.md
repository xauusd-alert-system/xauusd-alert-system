# NeuroBook (vendored) — book NN library for MQL5

**Status: TZ_BOOKS task T-01.** Version pinned by
[`NEUROBOOK_MANIFEST.json`](NEUROBOOK_MANIFEST.json), fetched by
[`fetch_neurobook.py`](fetch_neurobook.py). The source bytes are NOT committed
(licensing + repo size); the committed manifest is the version pin.

## What it is

`NeuroBook` is the public source-code project of the book
**“Neural Networks for Algorithmic Trading with MQL5”** (Dmitriy Gizlyk,
MetaQuotes Ltd), referenced by the book at page 5 as
`\\MQL5\Shared Projects\NeuroBook`. It contains:

| Component | Files | Role in this repo |
|---|---|---|
| MQL5 NN library (`Include/NeuroNetworksBook/realization/`) | `neuronbase.mqh`, `neuronnet.mqh`, `neuronattention.mqh`, `neuronmhattention.mqh`, `neuronlstm.mqh`, `neuronconv.mqh`, `neurongpt.mqh`, `neuronbatchnorm.mqh`, `neurondropout.mqh`, `layerdescription.mqh`, … | the `CLayerDescription` declarative layer API used by `mql5/NeuroTrader` when NN inference runs **inside the terminal** |
| OpenCL kernels | `opencl_program.cl`, `mult_vect_ocl.cl`, `opencl.mqh` | GPU kernels for T-18 (OpenCL inference benchmark) |
| EA template | `Experts/NeuroNetworksBook/ea_template.mq5` | reference for the TradeLevel EA pattern (T-04/T-10) |
| Sample generators | `Scripts/NeuroNetworksBook/initial_data/*.mq5` (`create_initial_data.mq5`, …) | reference pattern for T-02 |
| Gradient-check scripts | `check_gradient_percp.mq5`, `check_gradient_conv.mq5`, `check_gradient_lstm.mq5` | reference for T-11 |
| Python cross-checks | `perceptron.py`, `lstm.py`, `attention.py`, … | methodology mirrored by our own `model/book_nn/` package (Python side) |

## Pinned version

| Field | Value |
|---|---|
| CodeBase publication | https://www.mql5.com/en/code/48097 (published 2024-02-29) |
| Git mirror (MQL5 Algo Forge) | https://forge.mql5.io/rosh/NeuroBook |
| Pinned commit (branch `main`) | `67efcaf045b3b3ef3de907c7ac345626a6b38be6` (2026-02-23) |
| Shared project path | `\\MQL5\Shared Projects\NeuroBook` |

## Install

```bash
# 1. fetch + verify (writes mql5/NeuroBook/vendor/, never committed to git)
python mql5/NeuroBook/fetch_neurobook.py            # from the CodeBase ZIP
python mql5/NeuroBook/fetch_neurobook.py --from-forge  # or git clone @ pinned commit

# 2. verify an existing vendor tree against the manifest
python mql5/NeuroBook/fetch_neurobook.py --verify
```

Then copy the tree into the terminal:

```
%APPDATA%\MetaQuotes\Terminal\<INSTANCE>\MQL5\
    Include\NeuroNetworksBook\...
    Experts\NeuroNetworksBook\...
    Scripts\NeuroNetworksBook\...
```

or open the shared project `\\MQL5\Shared Projects\NeuroBook` directly in
MetaEditor (MQL5 Storage) and check out the pinned commit. `vendor/` is
git-ignored — see the `.gitignore` entry added with this module.

## Why a manifest instead of committed sources

1. The sandboxed/CI environment cannot reach mql5.com from bash, and fetching
   code through HTML converters corrupts it (BOM/escaping) — verbatim bytes
   must come from the official archive.
2. The sources are © MetaQuotes Ltd / Dmitriy Gizlyk, published for free
   download; redistributing them inside our git tree is unnecessary and bloats
   the repo by ~300 KiB of third-party code.
3. The manifest pins the exact version (forge commit + per-file sizes) so every
   machine can reproduce the identical tree; `--verify` fails loudly on drift.

## Relationship to `model/book_nn/` (Python)

Our Python pipeline cannot execute `.mqh` code, so the **methodology** of the
book (CLayerDescription-style declarative configs, Swish/Linear activations,
Adam, MSE, numerical gradient check, 60/20/20 splits, Train/Val monitoring,
MH-Attention/LSTM comparison) is re-implemented in pure NumPy under
`model/book_nn/` with gradient-check tests in CI (T-11). The vendored MQL5
library remains the reference implementation for terminal-side inference
(T-18 OpenCL) and the place to cross-check algorithms.
