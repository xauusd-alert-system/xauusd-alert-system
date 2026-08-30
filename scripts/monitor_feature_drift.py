"""
Feature drift monitoring (TZ 5.3 / P2-40): Population Stability Index (PSI)
per feature between a training feature matrix and a fresh live feature matrix.

Core API:

    from scripts.monitor_feature_drift import check_drift
    report = check_drift(train_features_df, live_features_df)
    # report["per_feature"] = {feature: psi}
    # report["max_psi"]     = float
    # report["drifted_features"] = [feature, ...]  (psi > 0.2)

CLI (two csv/parquet files with a shared set of feature columns):

    python -m scripts.monitor_feature_drift --train train.csv --live live.csv
    python -m scripts.monitor_feature_drift --train train.parquet --live live.parquet --json

PSI definition (standard, 10 quantile bins fitted on the TRAIN distribution):

    psi = sum_i (live_share_i - train_share_i) * ln(live_share_i / train_share_i)

Eps-protection prevents division/log of zero for empty bins (a live bin with
zero share is clamped to eps, which contributes a large but finite penalty —
exactly the behaviour you want when a feature's support has shifted).
"""

import argparse
import json
import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("monitor_feature_drift")

# Default drift threshold: PSI > 0.2 is the classical "significant shift"
# band (PSI < 0.1 stable, 0.1-0.2 moderate, > 0.2 drifted).
DRIFTED_PSI_THRESHOLD = 0.2
N_BINS = 10
_EPS = 1e-6


def _psi_for_feature(
    train_values: np.ndarray,
    live_values: np.ndarray,
    n_bins: int = N_BINS,
    eps: float = _EPS,
) -> float:
    """PSI between the train and live distributions of one feature.

    Bins are quantile-based and fitted on the TRAIN values only (10 bins by
    default). Degenerate train distributions (all-identical / all-NaN) yield
    0.0 when live is also degenerate on the same value, else a maximal
    penalty (1.0 * ln(1/eps)-scale is capped via the bin logic below).
    """
    train_clean = train_values[~np.isnan(train_values)]
    live_clean = live_values[~np.isnan(live_values)]
    if train_clean.size == 0 and live_clean.size == 0:
        return 0.0
    if train_clean.size == 0 or live_clean.size == 0:
        # No overlap is possible -> treat as maximal drift, keep it finite.
        return float((1.0 - eps) * np.log((1.0 - eps) / eps))

    # Quantile bin edges from TRAIN only.
    quantiles = np.linspace(0.0, 1.0, n_bins + 1)[1:-1]
    edges = np.unique(np.quantile(train_clean, quantiles))

    if edges.size == 0:
        # Degenerate train distribution (single unique value).
        train_val = float(train_clean[0])
        live_share_same = float(np.mean(live_clean == train_val))
        # Two-bin PSI: {== train_val, != train_val}.
        t_shares = np.array([1.0 - eps, eps])
        l_shares = np.array([max(live_share_same, eps), max(1.0 - live_share_same, eps)])
        return float(np.sum((l_shares - t_shares) * np.log(l_shares / t_shares)))

    # np.digitize with right=False: bins are (-inf, e0], (e0, e1], ... , (eN, +inf)
    train_bins = np.digitize(train_clean, edges, right=True)
    live_bins = np.digitize(live_clean, edges, right=True)
    n_actual = edges.size + 1

    t_counts = np.bincount(train_bins, minlength=n_actual).astype(float)
    l_counts = np.bincount(live_bins, minlength=n_actual).astype(float)
    t_shares = t_counts / max(t_counts.sum(), 1.0)
    l_shares = l_counts / max(l_counts.sum(), 1.0)

    # Eps-protection: clamp zero shares so the ratio/log never divide by zero.
    t_shares = np.clip(t_shares, eps, None)
    l_shares = np.clip(l_shares, eps, None)
    t_shares = t_shares / t_shares.sum()
    l_shares = l_shares / l_shares.sum()

    return float(np.sum((l_shares - t_shares) * np.log(l_shares / t_shares)))


def check_drift(
    train_features_df: pd.DataFrame,
    live_features_df: pd.DataFrame,
    features: Optional[list] = None,
    drifted_psi_threshold: float = DRIFTED_PSI_THRESHOLD,
) -> dict:
    """Compare a train feature matrix against fresh live features.

    Returns a drift report dict:
        {
          "per_feature": {feature: psi, ...},
          "max_psi": float,
          "drifted_features": [feature, ...],   # psi > threshold
          "drifted_psi_threshold": float,
          "n_features_checked": int,
        }
    """
    if not isinstance(train_features_df, pd.DataFrame) or not isinstance(live_features_df, pd.DataFrame):
        raise TypeError("check_drift expects two pandas DataFrames")

    common = [c for c in train_features_df.columns if c in live_features_df.columns]
    if features is not None:
        common = [c for c in common if c in set(features)]
    # Only numeric features can be binned.
    numeric_common = [
        c
        for c in common
        if pd.api.types.is_numeric_dtype(train_features_df[c]) and pd.api.types.is_numeric_dtype(live_features_df[c])
    ]
    if not numeric_common:
        return {
            "per_feature": {},
            "max_psi": 0.0,
            "drifted_features": [],
            "drifted_psi_threshold": float(drifted_psi_threshold),
            "n_features_checked": 0,
        }

    per_feature: dict = {}
    for col in numeric_common:
        per_feature[col] = _psi_for_feature(
            train_features_df[col].to_numpy(dtype=float),
            live_features_df[col].to_numpy(dtype=float),
        )

    drifted = sorted([f for f, psi in per_feature.items() if psi > drifted_psi_threshold])
    return {
        "per_feature": per_feature,
        "max_psi": max(per_feature.values()) if per_feature else 0.0,
        "drifted_features": drifted,
        "drifted_psi_threshold": float(drifted_psi_threshold),
        "n_features_checked": len(per_feature),
    }


def _load_table(path: str) -> pd.DataFrame:
    if path.lower().endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PSI feature-drift check between a train and a live feature matrix.")
    parser.add_argument("--train", required=True, help="Train features csv/parquet")
    parser.add_argument("--live", required=True, help="Live features csv/parquet")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DRIFTED_PSI_THRESHOLD,
        help=f"PSI above which a feature is flagged drifted (default {DRIFTED_PSI_THRESHOLD})",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    train_df = _load_table(args.train)
    live_df = _load_table(args.live)
    report = check_drift(train_df, live_df, drifted_psi_threshold=args.threshold)

    if args.json:
        print(json.dumps(report, indent=2, default=float))
    else:
        print(f"Features checked : {report['n_features_checked']}")
        print(f"Max PSI          : {report['max_psi']:.4f}")
        print(f"Drift threshold  : {report['drifted_psi_threshold']}")
        if report["drifted_features"]:
            print(f"DRIFTED ({len(report['drifted_features'])}):")
            for feat in report["drifted_features"]:
                print(f"  - {feat}: psi={report['per_feature'][feat]:.4f}")
            logger.warning(
                "Feature drift detected: %d feature(s) above PSI %.2f",
                len(report["drifted_features"]),
                args.threshold,
            )
        else:
            print("No drifted features.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    raise SystemExit(main())
