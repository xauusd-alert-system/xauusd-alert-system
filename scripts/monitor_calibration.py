"""
Calibration monitoring (TZ 5.3 / P2-46): Brier score and Expected Calibration
Error (ECE) for model probability predictions vs binary outcomes.

Core API:

    from scripts.monitor_calibration import check_calibration
    report = check_calibration(predictions, outcomes)
    # report["brier_score"]  = float
    # report["ece"]          = float
    # report["is_calibrated"] = bool  (ece <= 0.1)
    # report["reliability"]  = per-bin data (n, mean_pred, mean_outcome)

CLI (jsonl/csv with `pred` and `outcome` columns):

    python -m scripts.monitor_calibration --input preds.jsonl
    python -m scripts.monitor_calibration --input preds.csv --ece-threshold 0.1 --json

ECE definition: mean over 10 equal-width probability bins of
n_bin/N * |mean_pred_bin - mean_outcome_bin|.

Brier definition: mean((pred - outcome)^2).
"""

import argparse
import csv
import json
import logging
from typing import List

logger = logging.getLogger("monitor_calibration")

ECE_WARNING_THRESHOLD = 0.1
N_BINS = 10
_EPS = 1e-9


def _brier_score(predictions: List[float], outcomes: List[int]) -> float:
    if not predictions:
        raise ValueError("brier score requires at least one prediction")
    return float(sum((p - float(y)) ** 2 for p, y in zip(predictions, outcomes)) / len(predictions))


def _ece(
    predictions: List[float],
    outcomes: List[int],
    n_bins: int = N_BINS,
) -> tuple:
    """Return (ece, reliability) where reliability is a list of per-bin dicts:
    {"bin_low", "bin_high", "n", "mean_pred", "mean_outcome"} (empty bins are
    omitted)."""
    n = len(predictions)
    if n == 0:
        return 0.0, []
    bin_sums_pred = [0.0] * n_bins
    bin_sums_out = [0.0] * n_bins
    bin_counts = [0] * n_bins
    for p, y in zip(predictions, outcomes):
        p = min(max(float(p), 0.0), 1.0)
        b = min(int(p * n_bins), n_bins - 1)  # equal-width [0,1] bins
        bin_sums_pred[b] += p
        bin_sums_out[b] += float(y)
        bin_counts[b] += 1

    ece = 0.0
    reliability = []
    for b in range(n_bins):
        if bin_counts[b] == 0:
            continue
        mean_pred = bin_sums_pred[b] / bin_counts[b]
        mean_out = bin_sums_out[b] / bin_counts[b]
        ece += (bin_counts[b] / n) * abs(mean_pred - mean_out)
        reliability.append(
            {
                "bin_low": b / n_bins,
                "bin_high": (b + 1) / n_bins,
                "n": bin_counts[b],
                "mean_pred": mean_pred,
                "mean_outcome": mean_out,
            }
        )
    return float(ece), reliability


def check_calibration(
    predictions: List[float],
    outcomes: List[int],
    ece_threshold: float = ECE_WARNING_THRESHOLD,
) -> dict:
    """Brier score + ECE calibration report.

    Returns:
        {
          "brier_score": float,
          "ece": float,
          "ece_threshold": float,
          "is_calibrated": bool,     # ece <= threshold
          "n_predictions": int,
          "reliability": [...],      # per-bin reliability data
        }
    """
    if len(predictions) != len(outcomes):
        raise ValueError(f"predictions/outcomes length mismatch: {len(predictions)} vs {len(outcomes)}")
    predictions = [float(p) for p in predictions]
    outcomes = [int(y) for y in outcomes]
    if any(y not in (0, 1) for y in outcomes):
        raise ValueError("outcomes must be binary (0/1)")

    brier = _brier_score(predictions, outcomes)
    ece, reliability = _ece(predictions, outcomes)
    return {
        "brier_score": brier,
        "ece": ece,
        "ece_threshold": float(ece_threshold),
        "is_calibrated": ece <= ece_threshold,
        "n_predictions": len(predictions),
        "reliability": reliability,
    }


def _load_records(path: str) -> tuple:
    """Load (predictions, outcomes) from a .jsonl or .csv file with columns
    pred,outcome."""
    preds: List[float] = []
    outs: List[int] = []
    if path.lower().endswith(".jsonl"):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                preds.append(float(rec["pred"]))
                outs.append(int(rec["outcome"]))
    else:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for rec in reader:
                preds.append(float(rec["pred"]))
                outs.append(int(rec["outcome"]))
    return preds, outs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Brier + ECE calibration check on predictions vs outcomes.")
    parser.add_argument(
        "--input",
        required=True,
        help="jsonl/csv file with columns pred,outcome",
    )
    parser.add_argument(
        "--ece-threshold",
        type=float,
        default=ECE_WARNING_THRESHOLD,
        help=f"ECE above which the report flags miscalibration (default {ECE_WARNING_THRESHOLD})",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    preds, outs = _load_records(args.input)
    report = check_calibration(preds, outs, ece_threshold=args.ece_threshold)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Predictions     : {report['n_predictions']}")
        print(f"Brier score     : {report['brier_score']:.4f}")
        print(f"ECE             : {report['ece']:.4f}")
        print(f"ECE threshold   : {report['ece_threshold']}")
        print(f"Calibrated      : {report['is_calibrated']}")
        if not report["is_calibrated"]:
            logger.warning(
                "CALIBRATION WARNING: ECE %.4f > threshold %.2f",
                report["ece"],
                args.ece_threshold,
            )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    raise SystemExit(main())
