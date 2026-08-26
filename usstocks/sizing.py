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


def size_position(entry: float, stop: float, *,
                  risk_per_trade_usd: float = 10.0,
                  max_notional_usd: float = 5000.0) -> SizingResult:
    """Compute shares from the stop distance; validate all admission terms."""
    if entry <= 0 or stop <= 0:
        return SizingResult(False, "INVALID_PRICE_LEVELS")
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return SizingResult(False, "ZERO_RISK_DISTANCE", risk_per_share=0.0)

    shares_by_risk = math.floor(risk_per_trade_usd / risk_per_share)
    shares_by_notional = math.floor(max_notional_usd / entry)
    shares = min(shares_by_risk, shares_by_notional)

    if shares <= 0:
        if shares_by_notional <= 0:
            return SizingResult(False, "NOTIONAL_CAP_ZERO_SHARES",
                                risk_per_share=risk_per_share)
        return SizingResult(False, "RISK_CAP_ZERO_SHARES",
                            risk_per_share=risk_per_share)

    notional = shares * entry
    actual_risk = shares * risk_per_share
    if notional > max_notional_usd + 1e-9:
        return SizingResult(False, "NOTIONAL_CAP_EXCEEDED",
                            shares=shares, notional_usd=notional,
                            actual_risk_usd=actual_risk,
                            risk_per_share=risk_per_share)
    if actual_risk > risk_per_trade_usd + 1e-9:
        return SizingResult(False, "RISK_CAP_EXCEEDED",
                            shares=shares, notional_usd=notional,
                            actual_risk_usd=actual_risk,
                            risk_per_share=risk_per_share)
    return SizingResult(True, "OK", shares=shares, notional_usd=notional,
                        actual_risk_usd=actual_risk,
                        risk_per_share=risk_per_share)


def targets_from_r(side: str, entry: float, stop: float,
                   tp1_r: float = 1.0, tp2_r: float = 2.0) -> tuple[float, float]:
    """TP1/TP2 price levels from the R-multiples of the plan (ТЗ §7.5)."""
    risk = abs(entry - stop)
    sign = 1.0 if side == "long" else -1.0
    return entry + sign * tp1_r * risk, entry + sign * tp2_r * risk
