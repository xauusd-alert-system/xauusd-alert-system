"""Position sizing — ТЗ §8: technical stop FIRST, then share count.

Never size from full leverage. Caps: $10 planned risk and $5,000 notional
(config/us_stocks_challenge.yaml).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizingResult:
    ok: bool
    reason: str
    shares: int = 0
    notional_usd: float = 0.0
    actual_risk_usd: float = 0.0
    risk_per_share: float = 0.0
    estimated_commission_usd: float = 0.0


def size_position(entry: float, stop: float, *,
                  risk_per_trade_usd: float = 10.0,
                  max_notional_usd: float = 5000.0,
                  commission_per_share: float = 0.0,
                  fixed_commission: float = 0.0) -> SizingResult:
    """Compute shares from the stop distance and commission; validate all terms."""
    if entry <= 0 or stop <= 0:
        return SizingResult(False, "INVALID_PRICE_LEVELS")
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return SizingResult(False, "ZERO_RISK_DISTANCE", risk_per_share=0.0)

    # Round-trip commission per share (entry + exit)
    rt_commission_ps = 2.0 * max(0.0, commission_per_share)
    rt_fixed_fee = 2.0 * max(0.0, fixed_commission)

    net_risk_budget = risk_per_trade_usd - rt_fixed_fee
    if net_risk_budget <= 0:
        return SizingResult(False, "COMMISSION_EXCEEDS_RISK_BUDGET", risk_per_share=risk_per_share)

    total_risk_ps = risk_per_share + rt_commission_ps
    shares_by_risk = math.floor((net_risk_budget + 1e-9) / total_risk_ps)
    shares_by_notional = math.floor((max_notional_usd + 1e-9) / entry)
    shares = min(shares_by_risk, shares_by_notional)

    if shares <= 0:
        if shares_by_notional <= 0:
            return SizingResult(False, "NOTIONAL_CAP_ZERO_SHARES",
                                risk_per_share=risk_per_share)
        return SizingResult(False, "RISK_CAP_ZERO_SHARES",
                            risk_per_share=risk_per_share)

    notional = shares * entry
    est_commission = (shares * rt_commission_ps) + rt_fixed_fee
    actual_risk = (shares * risk_per_share) + est_commission
    if notional > max_notional_usd + 1e-9:
        return SizingResult(False, "NOTIONAL_CAP_EXCEEDED",
                            shares=shares, notional_usd=notional,
                            actual_risk_usd=actual_risk,
                            risk_per_share=risk_per_share,
                            estimated_commission_usd=est_commission)
    if actual_risk > risk_per_trade_usd + 1e-9:
        return SizingResult(False, "RISK_CAP_EXCEEDED",
                            shares=shares, notional_usd=notional,
                            actual_risk_usd=actual_risk,
                            risk_per_share=risk_per_share,
                            estimated_commission_usd=est_commission)
    return SizingResult(True, "OK", shares=shares, notional_usd=notional,
                        actual_risk_usd=actual_risk,
                        risk_per_share=risk_per_share,
                        estimated_commission_usd=est_commission)


def targets_from_r(side: str, entry: float, stop: float,
                   tp1_r: float = 1.0, tp2_r: float = 2.0) -> tuple[float, float]:
    """TP1/TP2 price levels from the R-multiples of the plan (ТЗ §7.5)."""
    risk = abs(entry - stop)
    sign = 1.0 if side == "long" else -1.0
    return entry + sign * tp1_r * risk, entry + sign * tp2_r * risk
