"""
Model Probability Calibration & Reliability Assessment (quant audit Section 5 / Task 4).

Evaluates whether the primary model's predicted probabilities are honest
probabilities or rank scores. Tree ensembles (XGBoost, RandomForest) are often
overconfident or uncalibrated out of the box.

Computes:
- Brier score: Mean squared error in probability space (0 = perfect, 0.25 = uninformative 50/50).
- Expected Calibration Error (ECE): Weighted average difference between predicted
  confidence and actual empirical win rate across discrete confidence bins.
- Reliability Diagram table: Bin-by-bin calibration curve data.
- Calibration Gate: If ECE > threshold (default 0.05), meta-labeling development
  is gated/blocked and an explicit warning is logged.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("model_calibration")


def compute_brier_score(y_true: np.ndarray | list | pd.Series, y_prob: np.ndarray | list | pd.Series) -> float:
    """Computes the Brier score: MSE of predicted probabilities vs binary outcomes."""
    y_t = np.asarray(y_true, dtype=float)
    y_p = np.asarray(y_prob, dtype=float)
    if len(y_t) == 0:
        return 0.0
    return float(np.mean((y_p - y_t) ** 2))


def compute_ece(
    y_true: np.ndarray | list | pd.Series, y_prob: np.ndarray | list | pd.Series, n_bins: int = 10
) -> tuple[float, list[dict]]:
    """Computes Expected Calibration Error (ECE) and bin-level reliability data.

    Parameters
    ----------
    y_true : binary outcomes {0, 1}
    y_prob : predicted probabilities in [0, 1]
    n_bins : number of equal-width bins (default 10)

    Returns
    -------
    ece : float Expected Calibration Error
    bins_data : list of dicts with bin-level metrics for reliability diagram
    """
    y_t = np.asarray(y_true, dtype=float)
    y_p = np.asarray(y_prob, dtype=float)
    n = len(y_t)
    if n == 0:
        return 0.0, []

    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins_data = []
    ece = 0.0

    for i in range(n_bins):
        low, high = bin_edges[i], bin_edges[i + 1]
        if i == n_bins - 1:
            mask = (y_p >= low) & (y_p <= high)
        else:
            mask = (y_p >= low) & (y_p < high)

        count = int(np.sum(mask))
        if count > 0:
            avg_pred = float(np.mean(y_p[mask]))
            actual_rate = float(np.mean(y_t[mask]))
            abs_diff = abs(actual_rate - avg_pred)
            ece += (count / n) * abs_diff
        else:
            avg_pred = float((low + high) / 2.0)
            actual_rate = 0.0
            abs_diff = 0.0

        bins_data.append(
            {
                "bin": i,
                "bin_lower": round(float(low), 3),
                "bin_upper": round(float(high), 3),
                "avg_predicted": round(avg_pred, 4),
                "actual_rate": round(actual_rate, 4),
                "count": count,
                "abs_error": round(abs_diff, 4),
            }
        )

    return float(ece), bins_data


def calibration_report(
    y_true: np.ndarray | list | pd.Series,
    y_prob: np.ndarray | list | pd.Series,
    n_bins: int = 10,
    ece_threshold: float = 0.05,
    asset_name: Optional[str] = None,
) -> dict[str, Any]:
    """Generates a full probability calibration diagnostic and applies the
    meta-labeling gate.

    Parameters
    ----------
    y_true : binary outcomes {0, 1}
    y_prob : predicted probabilities in [0, 1]
    n_bins : number of bins for ECE (default 10)
    ece_threshold : maximum acceptable ECE before gating meta-labeling (default 0.05)
    asset_name : optional asset label for log messages

    Returns
    -------
    dict with brier_score, ece, gate_passed, reliability_diagram, warning.
    """
    y_t = np.asarray(y_true, dtype=float)
    y_p = np.asarray(y_prob, dtype=float)

    brier = compute_brier_score(y_t, y_p)
    ece, bins_data = compute_ece(y_t, y_p, n_bins=n_bins)
    gate_passed = bool(ece <= ece_threshold)

    warning_msg = None
    if not gate_passed:
        asset_str = f" for {asset_name}" if asset_name else ""
        warning_msg = (
            f"CALIBRATION GATE FAILED{asset_str}: ECE={ece:.4f} > threshold={ece_threshold:.4f}. "
            "Primary model is poorly calibrated; meta-labeling development halted."
        )
        logger.warning(warning_msg)

    return {
        "asset": asset_name,
        "n_samples": len(y_t),
        "brier_score": round(brier, 4),
        "ece": round(ece, 4),
        "ece_threshold": float(ece_threshold),
        "gate_passed": gate_passed,
        "reliability_diagram": bins_data,
        "warning": warning_msg,
    }


def evaluate_asset_calibration(
    df: pd.DataFrame,
    cfg: dict,
    asset_key: str,
    n_bins: int = 10,
    ece_threshold: float = 0.05,
) -> dict[str, Any]:
    """Evaluates probability calibration on purged-OOS predictions for a single asset."""
    from backtest.walk_forward import generate_windows
    from scripts.deflated_sharpe import _score_fold

    wf_cfg = cfg["backtest"]["walk_forward"]
    windows = generate_windows(df, wf_cfg["train_window_days"], wf_cfg["test_window_days"], wf_cfg["step_days"])
    if not windows:
        return calibration_report([], [], n_bins=n_bins, ece_threshold=ece_threshold, asset_name=asset_key)

    all_y_true = []
    all_y_prob = []

    for w in windows:
        train_df = df[(df["timestamp_utc"] >= w.train_start_ts) & (df["timestamp_utc"] < w.train_end_ts)]
        test_df = df[(df["timestamp_utc"] >= w.test_start_ts) & (df["timestamp_utc"] < w.test_end_ts)]
        if len(test_df) == 0 or len(train_df) < 30:
            continue

        scored_test = _score_fold(train_df, test_df, cfg, asset_key)
        if "label" in scored_test.columns and "ml_p_long" in scored_test.columns:
            valid = scored_test.dropna(subset=["label", "ml_p_long"])
            # Binary +1 vs -1 or {1, 0}
            labels = (valid["label"].values == 1).astype(int)
            probs = valid["ml_p_long"].values.astype(float)
            all_y_true.extend(labels.tolist())
            all_y_prob.extend(probs.tolist())

    return calibration_report(all_y_true, all_y_prob, n_bins=n_bins, ece_threshold=ece_threshold, asset_name=asset_key)
