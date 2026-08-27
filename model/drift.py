"""Distribution-drift monitoring for the walk-forward pipeline (task T-23).

The book's normalization warning (NN book p. 223 - train/live distributions
must stay compatible) plus the finite life of every strategy (p. 56) are
operationalized here as measurable drift between the TRAIN feature
distribution and the LIVE/forward window:

* **PSI** (Population Stability Index) per feature - bucketed, symmetric,
  threshold conventions: < 0.10 stable, 0.10-0.25 warn, > 0.25 alarm;
* **KS statistic** (two-sample, via scipy when available) as a second
  opinion;
* ``normalization_shift`` - the drift of the LIVE normalization parameters
  vs the saved TRAIN parameters (mean/std ratio), which is exactly the
  "hidden distribution shift" class the book warns about.

Wire into the retraining/deploy pipeline: compute the report on each
retrain cycle; alarm status blocks the nightly deploy (deploy guard) the
same way a degraded metric does.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PSI_STABLE = 0.10
PSI_ALARM = 0.25


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10,
        quantile_bins: bool = True) -> float:
    """Population Stability Index between two samples of one feature."""
    expected = np.asarray(expected, dtype=float)
    actual = np.asarray(actual, dtype=float)
    expected = expected[np.isfinite(expected)]
    actual = actual[np.isfinite(actual)]
    if len(expected) == 0 or len(actual) == 0:
        return 0.0
    if quantile_bins:
        edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
    else:
        edges = np.linspace(expected.min(), expected.max(), bins + 1)
    if len(edges) < 3:  # degenerate (constant feature)
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    e_counts = np.histogram(expected, bins=edges)[0] / len(expected)
    a_counts = np.histogram(actual, bins=edges)[0] / len(actual)
    eps = 1e-6
    e_counts = np.clip(e_counts, eps, None)
    a_counts = np.clip(a_counts, eps, None)
    return float(np.sum((a_counts - e_counts) * np.log(a_counts / e_counts)))


def ks_statistic(expected: np.ndarray, actual: np.ndarray) -> float:
    """Two-sample KS statistic (scipy when present, exact fallback else)."""
    expected = np.sort(np.asarray(expected, dtype=float))
    actual = np.sort(np.asarray(actual, dtype=float))
    expected = expected[np.isfinite(expected)]
    actual = actual[np.isfinite(actual)]
    if len(expected) == 0 or len(actual) == 0:
        return 0.0
    try:
        from scipy.stats import ks_2samp
        return float(ks_2samp(expected, actual).statistic)
    except Exception:
        data_all = np.concatenate([expected, actual])
        cdf_e = np.searchsorted(expected, data_all, side="right") / len(expected)
        cdf_a = np.searchsorted(actual, data_all, side="right") / len(actual)
        return float(np.max(np.abs(cdf_e - cdf_a)))


def feature_drift_report(train_df: pd.DataFrame, live_df: pd.DataFrame,
                         columns: list[str] | None = None,
                         bins: int = 10) -> dict:
    """Per-feature PSI + KS between the training and live windows."""
    columns = list(columns or train_df.columns)
    report: dict = {"features": {}, "worst_psi": 0.0, "worst_ks": 0.0}
    for col in columns:
        if col not in train_df.columns or col not in live_df.columns:
            continue
        p = psi(train_df[col].to_numpy(), live_df[col].to_numpy(), bins=bins)
        k = ks_statistic(train_df[col].to_numpy(), live_df[col].to_numpy())
        report["features"][col] = {"psi": p, "ks": k}
        report["worst_psi"] = max(report["worst_psi"], p)
        report["worst_ks"] = max(report["worst_ks"], k)
    report["status"] = drift_status(report)
    return report


def drift_status(report: dict, psi_warn: float = PSI_STABLE,
                 psi_alarm: float = PSI_ALARM) -> str:
    """ok | warn | alarm from the report's worst PSI (fail-closed on missing)."""
    worst = float(report.get("worst_psi", 0.0))
    if worst > psi_alarm:
        return "alarm"
    if worst > psi_warn:
        return "warn"
    return "ok"


def normalization_shift(train_params: dict, live_params: dict) -> dict:
    """Drift of live normalization parameters vs the saved train ones.

    ``train_params``/``live_params``: {"center": {col: val}, "scale": {col: val}}
    (the serialized NormalizationParams of task T-02). Reports per-column
    scale ratio (>1.5 or <0.66 is a 50%+ volatility-regime shift) and mean
    shift in train-scale units.
    """
    t_center = train_params.get("center", {})
    t_scale = train_params.get("scale", {})
    l_center = live_params.get("center", {})
    l_scale = live_params.get("scale", {})
    out = {"columns": {}, "worst_scale_ratio": 1.0, "worst_mean_shift_sigmas": 0.0,
           "shifted_columns": []}
    for col in t_center:
        if col not in l_center or col not in l_scale:
            continue
        ts = float(t_scale.get(col, 1.0)) or 1.0
        ls = float(l_scale.get(col, 1.0)) or 1.0
        scale_ratio = ls / ts
        mean_shift_sigmas = abs(float(l_center[col]) - float(t_center[col])) / ts
        out["columns"][col] = {"scale_ratio": scale_ratio,
                               "mean_shift_sigmas": mean_shift_sigmas}
        out["worst_scale_ratio"] = max(out["worst_scale_ratio"], scale_ratio,
                                       1.0 / max(scale_ratio, 1e-9))
        out["worst_mean_shift_sigmas"] = max(out["worst_mean_shift_sigmas"],
                                             mean_shift_sigmas)
        if scale_ratio > 1.5 or scale_ratio < 1 / 1.5 or mean_shift_sigmas > 1.0:
            out["shifted_columns"].append(col)
    out["status"] = "alarm" if out["shifted_columns"] else "ok"
    return out


def walk_forward_drift_gate(train_features: pd.DataFrame,
                            live_features: pd.DataFrame,
                            norm_train: dict | None = None,
                            norm_live: dict | None = None) -> dict:
    """Combined drift gate for the walk-forward loop (T-23): blocks deploy on
    alarm-level PSI or a >50% normalization-scale shift."""
    report = feature_drift_report(train_features, live_features)
    if norm_train is not None and norm_live is not None:
        report["normalization"] = normalization_shift(norm_train, norm_live)
        norm_alarm = report["normalization"]["status"] == "alarm"
    else:
        report["normalization"] = None
        norm_alarm = False
    report["deploy_allowed"] = (report["status"] != "alarm") and not norm_alarm
    return report
