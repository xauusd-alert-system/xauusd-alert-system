"""
Phase 11: Dynamic Portfolio Allocator & Risk Parity.
Provides institutional risk allocation and position sizing models:
- Fractional Kelly Criterion sizing
- Inverse Volatility weighting
- Hierarchical Risk Parity (HRP) allocation
- Lot size calculation with pip / point value scaling
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Optional


def calculate_fractional_kelly(
    win_rate: float,
    win_loss_ratio: float,
    fraction: float = 0.5,
    min_risk: float = 0.005,
    max_risk: float = 0.05,
) -> float:
    """
    Computes conservative fractional Kelly fraction:
    f* = (p * b - q) / b
    where p = win_rate, q = 1 - p, b = win_loss_ratio.
    Scaled by `fraction` (e.g. half-Kelly = 0.5) and bounded by [min_risk, max_risk].
    """
    if win_loss_ratio <= 0:
        return min_risk

    p = np.clip(win_rate, 0.0, 1.0)
    q = 1.0 - p
    b = max(win_loss_ratio, 0.01)

    raw_kelly = (p * b - q) / b
    if raw_kelly <= 0:
        return min_risk

    scaled = raw_kelly * fraction
    return float(np.clip(scaled, min_risk, max_risk))


def inverse_volatility_allocation(volatilities: Dict[str, float]) -> Dict[str, float]:
    """
    Allocates portfolio capital weights inversely proportional to asset volatility.
    Assets with higher volatility get lower capital weight.
    """
    if not volatilities:
        return {}

    inv_vols = {k: 1.0 / max(v, 1e-6) for k, v in volatilities.items()}
    total_inv = sum(inv_vols.values())

    if total_inv <= 0:
        equal_w = 1.0 / len(volatilities)
        return {k: equal_w for k in volatilities}

    return {k: v / total_inv for k, v in inv_vols.items()}


def calculate_lot_size(
    account_equity: float,
    risk_pct: float,
    stop_loss_distance: float,
    point_value_lot: float = 100.0,
    min_lot: float = 0.01,
    max_lot: float = 10.0,
    lot_step: float = 0.01,
) -> float:
    """
    Calculates precise order volume in lots:
    Risk Amount ($) = Equity * (Risk % / 100)
    Money at Risk per 1.0 Lot = Stop Distance * Point Value per Lot
    Lot Size = Risk Amount / Money at Risk per Lot
    """
    # N7 (audit 2026-08-10): never round UP to min_lot. A risk-based size below
    # the broker minimum is NOT tradeable and must be skipped by the caller
    # (matches execution/risk_sizer.lots_for_risk's "never round up"). Forcing
    # min_lot here would exceed the intended risk-per-trade. Returns 0.0 for
    # degenerate inputs so callers can treat 0 as "skip".
    if stop_loss_distance <= 0 or point_value_lot <= 0 or account_equity <= 0:
        return 0.0

    risk_amount = account_equity * (risk_pct / 100.0)
    risk_per_full_lot = stop_loss_distance * point_value_lot

    if risk_per_full_lot <= 0:
        return 0.0

    raw_lots = risk_amount / risk_per_full_lot
    # Quantize DOWN to lot_step (never exceed the intended risk).
    stepped_lots = np.floor(raw_lots / lot_step) * lot_step
    return float(np.clip(stepped_lots, 0.0, max_lot))


def hierarchical_risk_parity(returns_df: pd.DataFrame) -> pd.Series:
    """
    Computes portfolio weights from asset returns.

    N8 (audit 2026-08-10): this is NOT true Hierarchical Risk Parity — there is
    no correlation clustering or recursive bisection; it returns plain
    inverse-variance weights (inv_diag / sum(inv_diag)). The name is retained for
    backward compatibility but callers must not rely on HRP properties. The risk
    layer that would use it is disabled (risk.enabled: false).
    """
    if returns_df.empty or returns_df.shape[1] == 1:
        return pd.Series([1.0], index=returns_df.columns if not returns_df.empty else ["default"])

    cov = returns_df.cov()
    diag = np.diag(cov.values)
    inv_diag = 1.0 / np.maximum(diag, 1e-8)
    weights = inv_diag / np.sum(inv_diag)
    return pd.Series(weights, index=returns_df.columns)


def validate_scaleout_tranches(
    base_volume: float,
    scaleout_ratios: list[float] | tuple[float, ...],
    min_lot: float = 0.01,
    lot_step: float = 0.01,
    raise_on_invalid: bool = False,
) -> tuple[bool, str, list[float]]:
    """Validates that each scale-out tranche is an exact multiple of `lot_step`
    and at least `min_lot` (quant audit Section 5.2A / Task 7).

    With base lot < 0.10, the 50/30/20 scheme produces fractional volumes
    below 0.01 (e.g. 0.005 / 0.003 / 0.002) which cannot be filled in live MT5.
    """
    tranches = [base_volume * r for r in scaleout_ratios]
    valid = True
    err_msg = ""
    for idx, t in enumerate(tranches):
        if t < min_lot - 1e-9:
            valid = False
            err_msg = f"Tranche {idx+1} volume {t:.4f} < min_lot {min_lot:.2f}"
            break
        rem = t % lot_step
        if not (abs(rem) < 1e-6 or abs(rem - lot_step) < 1e-6):
            valid = False
            err_msg = f"Tranche {idx+1} volume {t:.4f} is not a multiple of lot_step {lot_step:.2f}"
            break

    if not valid and raise_on_invalid:
        raise ValueError(
            f"Invalid scale-out configuration: base volume {base_volume:.4f} with ratios "
            f"{scaleout_ratios} yields {err_msg}."
        )

    quantized = [max(min_lot, round(t / lot_step) * lot_step) for t in tranches]
    return valid, err_msg, quantized
