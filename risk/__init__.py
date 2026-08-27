"""risk — unified Risk Engine package (ТЗ 8.5).

Public API:
    RiskEngine        — the single pre-trade entry point:
                        ``can_open(...)`` / ``can_trade(...)`` (alias);
    RiskLimits        — daily limits, concurrency caps, circuit breaker
                        (P0-5 exclude_swaps) — THE single source of daily
                        limits (P2-10);
    RateThrottle      — rate-based throttling only (N orders/minute, P2-10);
    RiskState         — persistent state: HWM, daily anchors, counters;
    InstitutionalRiskManager — backwards-compatible wrapper (historical
                        API name, see risk/compat.py);
    sizing functions  — re-exported from risk.sizing (lots_for_risk,
                        cluster_exposure_ok with REQUIRED caps (P1-4),
                        drawdown_throttle (P1-7 HWM semantics), ...).

Module map (moved from execution/ — the old files remain as deprecated
shims until the cleanup phase):
    risk/limits.py         ← execution/risk_manager.py
    risk/sizing.py         ← execution/risk_sizer.py
    risk/legacy_throttle.py← execution/trade_throttle.py (deprecated class)
    risk/throttle.py       ← NEW: rate-based only (P2-10)
    risk/state.py          ← persistence extracted from risk_manager.py
    risk/engine.py         ← NEW: facade aggregating all gates

Example::

    from risk import RiskEngine
    engine = RiskEngine(cfg, magic=777111)
    ok, reason = engine.can_open("XAUUSD", equity=10_000)
"""
from risk.compat import InstitutionalRiskManager
from risk.engine import RiskEngine
from risk.limits import RiskLimits
from risk.state import RiskState
from risk.throttle import RateThrottle

# Sizing convenience re-exports (the full set lives in risk.sizing).
from risk.sizing import (  # noqa: F401
    cluster_exposure_ok,
    drawdown_throttle,
    leverage_multiplier,
    lots_for_risk,
    risk_config,
    same_direction_cluster_penalty,
    trade_risk_pct,
    vol_target_scale,
)

__all__ = [
    "RiskEngine",
    "RiskLimits",
    "RateThrottle",
    "RiskState",
    "InstitutionalRiskManager",
    "cluster_exposure_ok",
    "drawdown_throttle",
    "leverage_multiplier",
    "lots_for_risk",
    "risk_config",
    "same_direction_cluster_penalty",
    "trade_risk_pct",
    "vol_target_scale",
]
