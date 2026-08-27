"""run_book_drift_report - T-23 drift gates measured on the real candle store.

The real-data experiment (see docs/BOOKS_INTEGRATION_REPORT.md, section 3)
found a strong regime shift: test-window target variance [1.87, 2.14] vs 1.0
on train (the 2025 gold rally). This script quantifies exactly that with the
book drift toolkit, on the SAME chronological 60/20/20 split the training
pipeline uses, so the numbers are directly comparable:

* ``feature_drift_report`` - per-feature PSI + KS, train slice vs test slice;
* ``normalization_shift`` - live-fitted normalization parameters vs the
  train-fitted ones (scale ratio >1.5 or mean shift >1 sigma = alarm);
* ``walk_forward_drift_gate`` - the deploy gate: blocks on alarm-level PSI
  or a >50% normalization-scale shift.

Usage::

    python -m scripts.run_book_drift_report \
        --db data/market_data_external.sqlite --asset XAUUSD \
        --timeframe M15 --max-bars 80000 \
        --out output/book_drift_report/report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from model.drift import (  # noqa: E402
    feature_drift_report,
    normalization_shift,
    walk_forward_drift_gate,
)
from model.sample_generator import (  # noqa: E402
    DEFAULT_CFG,
    FEATURE_COLUMNS_BASE,
    FEATURE_COLUMNS_EXTENDED,
    build_book_features,
    fit_normalization,
)
from scripts.run_book_experiments import load_candles  # noqa: E402

logger = logging.getLogger("run_book_drift_report")


def build_report(df, cfg: dict) -> dict:
    """Drift report for train-vs-test feature frames of one candle frame."""
    features = build_book_features(df, cfg)
    cols = (FEATURE_COLUMNS_EXTENDED if cfg["extended"]
            else FEATURE_COLUMNS_BASE)
    features = features[cols].replace([np.inf, -np.inf], np.nan).dropna()

    ratios = (cfg["split"]["train"], cfg["split"]["valid"], cfg["split"]["test"])
    from model.sample_generator import split_indices_time_ordered
    tr_end, va_end = split_indices_time_ordered(len(features), ratios)
    train_f = features.iloc[:tr_end]
    live_f = features.iloc[va_end:]          # the test slice = "live"

    report = feature_drift_report(train_f, live_f)
    norm_train = fit_normalization(train_f).to_dict()
    norm_live = fit_normalization(live_f).to_dict()
    shift = normalization_shift(norm_train, norm_live)
    gate = walk_forward_drift_gate(train_f, live_f,
                                   norm_train=norm_train, norm_live=norm_live)

    return {
        "bars": int(len(df)),
        "features": list(cols),
        "train_rows": int(len(train_f)),
        "live_rows": int(len(live_f)),
        "psi_ks": report,
        "normalization_shift": shift,
        "gate": gate,
        "gate_deploy_blocked": bool(gate.get("status") == "alarm"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/market_data_external.sqlite")
    ap.add_argument("--asset", default="XAUUSD")
    ap.add_argument("--timeframe", default="M15")
    ap.add_argument("--max-bars", type=int, default=80000,
                    help="same tail slice as the training run (comparability)")
    ap.add_argument("--extended-features", action="store_true")
    ap.add_argument("--out", default="output/book_drift_report/report.json")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    df = load_candles(args.asset, args.timeframe, args.db)
    if df is None:
        ap.error(f"no {args.asset} {args.timeframe} candles in {args.db}")
    if args.max_bars and len(df) > args.max_bars:
        df = df.tail(int(args.max_bars)).reset_index(drop=True)

    cfg = {**DEFAULT_CFG, "extended": bool(args.extended_features)}
    report = build_report(df, cfg)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    psi = report["psi_ks"]
    shift = report["normalization_shift"]
    print(json.dumps({
        "bars": report["bars"],
        "worst_psi": round(psi["worst_psi"], 4),
        "worst_ks": round(psi["worst_ks"], 4),
        "feature_status": psi["status"],
        "worst_scale_ratio": round(shift["worst_scale_ratio"], 4),
        "worst_mean_shift_sigmas": round(shift["worst_mean_shift_sigmas"], 4),
        "shifted_columns": shift["shifted_columns"],
        "gate_status": report["gate"]["status"],
        "deploy_blocked": report["gate_deploy_blocked"],
    }, indent=2))
    logger.info("report -> %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
