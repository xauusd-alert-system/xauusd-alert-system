"""Risk sizing (ТЗ 8.5 → moved from ``execution/risk_sizer.py``).

Responsibility:
    Pure, stateless sizing/exposure math:

    - pre-trade stop-risk sizing (`lots_for_risk`, `trade_risk_pct`);
    - correlated-cluster and total exposure caps (`cluster_exposure_ok`);
    - same-direction cluster penalty;
    - drawdown throttle multiplier (P1-7: measured against the *persistent*
      equity HWM — the HWM itself lives in ``risk/state.py``, not here);
    - volatility targeting (`leverage_multiplier`, `vol_target_scale`).

Inputs / outputs:
    Plain functions, no MT5 access, no file I/O. ``cluster_exposure_ok``
    requires ``cluster_cap`` and ``total_cap`` explicitly (P1-4 — no silent
    defaults in the signature; callers must pass the caps from config).

Dependencies:
    numpy / pandas only.

Example::

    from risk.sizing import cluster_exposure_ok, drawdown_throttle

    check = cluster_exposure_ok({"metals": 0.001}, "metals", 0.002,
                                cluster_cap=0.004, total_cap=0.0075)
    if not check["ok"]:
        skip_trade()

    mult = drawdown_throttle(dd_from_hwm=equity / hwm - 1.0)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def trade_risk_pct(target_ann_vol: float, sigma_r: float, trades_per_day: float,
                   enb: float = 1.0, periods_per_year: float = 250.0) -> float:
    """Per-trade risk (% of equity) implied by the portfolio vol target:

        risk_pct = target_vol / (sigma_r * sqrt(trades_per_day * periods_per_year) * enb_adjust)

    enb < n_traded_assets shrinks the total risk (fewer effective bets), so
    each trade carries a smaller share. Returns a fraction (0.002 = 0.2%).
    """
    if sigma_r <= 0 or trades_per_day <= 0:
        return 0.0
    enb = max(enb, 1e-6)
    per_day_risk = target_ann_vol / (sigma_r * np.sqrt(periods_per_year))
    return float(per_day_risk / (np.sqrt(trades_per_day) * np.sqrt(enb)))


def lots_for_risk(equity: float, risk_pct: float, sl_ticks: float,
                  tick_value_per_lot: float, min_lot: float = 0.01) -> dict:
    """Lot size so the full SL costs `risk_pct` of equity:

        lots = equity * risk_pct / (sl_ticks * tick_value_per_lot)

    If lots < min_lot the trade must be SKIPPED (audit: never round up).
    Returns {lots, skipped, reason}.
    """
    if equity <= 0 or risk_pct <= 0 or sl_ticks <= 0 or tick_value_per_lot <= 0:
        return {"lots": 0.0, "skipped": True, "reason": "non-positive inputs"}
    lots = float(equity * risk_pct / (sl_ticks * tick_value_per_lot))
    if lots < min_lot:
        return {"lots": lots, "skipped": True,
                "reason": f"computed lot {lots:.4f} < minimum {min_lot}; skip (never round up)"}
    return {"lots": lots, "skipped": False, "reason": "ok"}


def cluster_exposure_ok(current_risk_by_cluster: dict[str, float],
                        cluster: str, add_risk_pct: float,
                        cluster_cap: float, total_cap: float) -> dict:
    """Correlated-exposure cap (P1-4: caps are REQUIRED parameters — no
    defaults in the signature, callers must pass the configured caps).

    Adding `add_risk_pct` to `cluster` must keep the cluster sum <=
    `cluster_cap` and the grand total <= `total_cap`.
    """
    cluster_sum = float(current_risk_by_cluster.get(cluster, 0.0)) + add_risk_pct
    total_sum = float(sum(current_risk_by_cluster.values())) + add_risk_pct
    ok = cluster_sum <= cluster_cap and total_sum <= total_cap
    return {"ok": ok, "cluster_sum": cluster_sum, "total_sum": total_sum,
            "cluster_cap": cluster_cap, "total_cap": total_cap}


def same_direction_cluster_penalty(current_dir: dict[str, int], asset: str,
                                   direction: int, cluster: str,
                                   multiplier: float = 0.35) -> float:
    """New same-direction signal inside an already-exposed cluster: multiply
    the size by 0.35-0.50 (audit) instead of rejecting outright."""
    exposed = current_dir.get(cluster)
    if exposed is not None and exposed == direction:
        return multiplier
    return 1.0


def drawdown_throttle(dd_from_hwm: float,
                      levels=((-0.04, 0.75), (-0.06, 0.50), (-0.08, 0.0))) -> float:
    """Risk multiplier from the drawdown vs high-water mark: -4% -> 0.75,
    -6% -> 0.50, -8% -> 0.0 (no new live entries, shadow only).

    P1-7: the argument is the CURRENT drawdown against the persistent HWM
    (see ``RiskState.hwm`` in ``risk/state.py``). The HWM does not reset on a
    calendar-day change, so a throttle level once engaged stays engaged until
    equity makes a new high.
    """
    dd = min(0.0, float(dd_from_hwm))
    mult = 1.0
    # most-negative level first; the first satisfied threshold wins
    for level, m in sorted(levels):
        if dd <= level:
            mult = m
            break
    return mult


def leverage_multiplier(target_ann_vol: float, ewma_vol_20d: float,
                        lo: float = 0.5, hi: float = 1.25) -> float:
    """Vol targeting leverage: clip(lo, hi, target / EWMA20d vol)."""
    vol = np.asarray(ewma_vol_20d, dtype=float)
    if vol.size == 0 or np.all(vol <= 0):
        return 1.0 if vol.ndim == 0 else np.ones_like(vol)
    out = np.clip(target_ann_vol / vol, lo, hi)
    if vol.ndim == 0:
        return float(out)
    return out


def vol_target_scale(returns: np.ndarray, target_ann_vol: float,
                     periods_per_year: float = 250.0,
                     ewma_span: int = 20) -> np.ndarray:
    """Per-day ex-ante scale = leverage_multiplier(target, EWMA vol of the
    trailing `ewma_span` returns), 1.0 for the warm-up window."""
    r = np.asarray(returns, dtype=float)
    out = np.ones(len(r))
    if len(r) < 2:
        return out
    s = pd.Series(r).ewm(span=ewma_span, adjust=False).std().to_numpy()
    ann = s * np.sqrt(periods_per_year)
    out[ann > 0] = leverage_multiplier(target_ann_vol, ann[ann > 0])
    return out


def risk_config(cfg: dict) -> dict:
    """Effective risk config (top-level `risk:` block with per-asset override
    assets.<key>.risk merging)."""
    base = cfg.get("risk", {}) or {}
    return base
