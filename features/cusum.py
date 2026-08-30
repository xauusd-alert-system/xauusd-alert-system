"""
CUSUM change-point detection on log returns (Задача 3.2).

**P2 regime/abstention feature — NOT direction alpha.** Per
docs/MQL5_OBSERVER_PLAN.md: on financial data empirical false positives are
far above theoretical prediction, and the detector flags mostly VOLATILITY
breaks, not direction. The four columns produced here are intended for
regime/abstention experiments (Задача 3.3 feature-selection gate decides
admission), never as a directional signal by themselves.

Design (preregistered by the owner, 2026-08-29 — do NOT tune against
backtest outcomes; recalibration is a separate registered study):

    r_t        = log(close_t / close_{t-1})
    sigma_t    = rolling std(r, window=roll_sigma_window, min_periods=window)
                 (strictly causal: bar t uses r <= t only)
    drift_t    = drift_sigma     * sigma_t
    thresh_t   = threshold_sigma * sigma_t
    S+_t       = max(0, S+_{t-1} + r_t - drift_t)
    S-_t       = max(0, S-_{t-1} - r_t - drift_t)
    change-point up:   S+_t > thresh_t -> sign +1, S+ reset to 0
    change-point down: S-_t > thresh_t -> sign -1, S- reset to 0

Preregistered h/k defaults (config features.cusum):
    roll_sigma_window: 96   (M15 = 24h)
    threshold_sigma:   3.0
    drift_sigma:       0.5

Causality contract: the recurrent loop consumes only data with index <= t.
NaN/inf in the input produce NaN columns for the affected rows and freeze
(no update of) the S-statistics — never an exception, never a silent zero.
sigma = 0 (constant series) -> norm columns NaN, S stays 0, no change-points.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_ROLL_SIGMA_WINDOW = 96
DEFAULT_THRESHOLD_SIGMA = 3.0
DEFAULT_DRIFT_SIGMA = 0.5

CUSUM_COLUMNS = ["cp_bars_since", "cp_last_sign", "cusum_up_norm", "cusum_down_norm"]


def _log_returns(close: pd.Series) -> np.ndarray:
    """Causal log returns; non-finite closes -> NaN return, never an exception."""
    c = pd.to_numeric(close, errors="coerce").astype(float).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.log(c[1:] / c[:-1])
    return np.concatenate([[np.nan], r])


def cusum_series(
    log_returns: pd.Series | np.ndarray,
    roll_sigma_window: int = DEFAULT_ROLL_SIGMA_WINDOW,
    threshold_sigma: float = DEFAULT_THRESHOLD_SIGMA,
    drift_sigma: float = DEFAULT_DRIFT_SIGMA,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Two-sided causal CUSUM over a log-return series.

    Returns (S_plus, S_minus, cp_sign, valid_mask) as float/int ndarrays of
    the same length as the input, where:

    * ``S_plus`` / ``S_minus`` — the raw CUSUM statistics (0 during warm-up
      and after each reset);
    * ``cp_sign`` — +1 on a change-point-up bar, -1 on change-point-down,
      0 otherwise (and throughout warm-up);
    * ``valid_mask`` — 1.0 where sigma_t is finite and positive (the bar has
      a defined drift/threshold), else 0.0.

    NaN/inf returns freeze the statistics for that bar (S unchanged, no CP).
    sigma = 0 (constant series) keeps S at 0 with no change-points.
    """
    if not isinstance(roll_sigma_window, (int, np.integer)) or roll_sigma_window < 2:
        raise ValueError(f"roll_sigma_window must be an integer >= 2, got {roll_sigma_window!r}")
    for name, val in (("threshold_sigma", threshold_sigma), ("drift_sigma", drift_sigma)):
        if not np.isfinite(val) or val <= 0:
            raise ValueError(f"{name} must be finite and positive, got {val!r}")

    r = np.asarray(log_returns, dtype=float)
    n = len(r)
    s_plus = np.zeros(n)
    s_minus = np.zeros(n)
    cp_sign = np.zeros(n, dtype=int)
    valid = np.zeros(n)

    if n == 0:
        return s_plus, s_minus, cp_sign, valid

    rs = pd.Series(r)
    sigma = rs.rolling(roll_sigma_window, min_periods=roll_sigma_window).std().to_numpy()

    for t in range(n):
        sig = sigma[t]
        if not np.isfinite(sig) or sig <= 0:
            # Warm-up, degenerate sigma, or NaN input return: freeze.
            s_plus[t] = s_plus[t - 1] if t > 0 else 0.0
            s_minus[t] = s_minus[t - 1] if t > 0 else 0.0
            cp_sign[t] = 0
            continue

        valid[t] = 1.0
        r_t = r[t]
        if not np.isfinite(r_t):
            s_plus[t] = s_plus[t - 1]
            s_minus[t] = s_minus[t - 1]
            continue

        drift = drift_sigma * sig
        thresh = threshold_sigma * sig

        sp = max(0.0, s_plus[t - 1] + r_t - drift)
        sm = max(0.0, s_minus[t - 1] - r_t - drift)

        if sp > thresh:
            cp_sign[t] = 1
            sp = 0.0
        elif sm > thresh:
            cp_sign[t] = -1
            sm = 0.0

        s_plus[t] = sp
        s_minus[t] = sm

    return s_plus, s_minus, cp_sign, valid


def cusum_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Append the 4 CUSUM columns (CUSUM_COLUMNS) to a featured frame.

    Reads ``features.cusum`` from cfg; ``enabled`` MUST have been checked by
    the caller (build_full_df gate) — this function assumes it is on.
    Price column: ``close`` (log returns inside).
    """
    cusum_cfg = (cfg.get("features", {}).get("cusum", {}) or {})
    window = int(cusum_cfg.get("roll_sigma_window", DEFAULT_ROLL_SIGMA_WINDOW))
    threshold_sigma = float(cusum_cfg.get("threshold_sigma", DEFAULT_THRESHOLD_SIGMA))
    drift_sigma = float(cusum_cfg.get("drift_sigma", DEFAULT_DRIFT_SIGMA))

    out = df.copy()
    n = len(out)
    if n == 0:
        for col in CUSUM_COLUMNS:
            out[col] = pd.Series(dtype=float) if col != "cp_last_sign" else pd.Series(dtype=int)
        return out

    r = _log_returns(out["close"])
    s_plus, s_minus, cp_sign, valid = cusum_series(r, window, threshold_sigma, drift_sigma)

    sigma = (
        pd.Series(r)
        .rolling(window, min_periods=window)
        .std()
        .to_numpy()
    )
    last_cp_idx = -1
    last_sign = 0
    bars_since = np.full(n, np.nan)

    for t in range(n):
        if cp_sign[t] != 0:
            last_cp_idx = t
            last_sign = int(cp_sign[t])
        if valid[t] and last_cp_idx >= 0:
            bars_since[t] = float(t - last_cp_idx)

    out["cp_bars_since"] = bars_since
    # cp_last_sign carries the sign of the LAST change-point so far (0 if none);
    # computed via running last, not per-bar CP sign.
    sign_series = np.zeros(n, dtype=int)
    cur = 0
    for t in range(n):
        if cp_sign[t] != 0:
            cur = int(cp_sign[t])
        sign_series[t] = cur
    out["cp_last_sign"] = sign_series

    with np.errstate(divide="ignore", invalid="ignore"):
        out["cusum_up_norm"] = np.where(
            (valid > 0) & np.isfinite(sigma) & (sigma > 0), s_plus / sigma, np.nan
        )
        out["cusum_down_norm"] = np.where(
            (valid > 0) & np.isfinite(sigma) & (sigma > 0), s_minus / sigma, np.nan
        )
    return out
