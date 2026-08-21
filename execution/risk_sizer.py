"""
Risk engine (quant audit 2026-08-07, Claude plan question 6, priorities 1-3).

Order of importance for survival (from the audit):
  1. pre-trade stop-risk sizing + exposure caps   (limits the loss BEFORE it
     exists);
  2. volatility targeting                         (reacts earlier than DD; note:
     ATR stops already embed implicit vol targeting, so the marginal gain is
     small — implemented but optional);
  3. drawdown throttle from high-water mark       (protects against model
     degradation; lagging);
  4. portfolio hard stop                          (last-resort barrier).

Audit starting parameters (config `risk:`):
  - risk per trade (full SL): 0.20-0.25% of equity
  - correlated-cluster stop-risk cap: <= 0.40%
  - total open stop-risk: <= 0.75%
  - portfolio vol target 8-10% annualized, leverage clip(0.5, 1.25, target /
    EWMA20d vol)
  - new same-direction signal in the same cluster: reject or size x 0.35-0.50
  - drawdown throttle: -4% -> 0.75, -6% -> 0.50, -8% -> no new entries

Lot formula (audit): lots = (equity x allowed risk) / (SL ticks x tick value
per lot); if the minimum lot exceeds the risk limit, SKIP the trade (never
round up to 0.01).
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
                        cluster_cap: float = 0.004,
                        total_cap: float = 0.0075) -> dict:
    """Correlated-exposure cap: adding `add_risk_pct` to `cluster` must keep
    the cluster sum <= cluster_cap and the grand total <= total_cap."""
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
    -6% -> 0.50, -8% -> 0.0 (no new live entries, shadow only)."""
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
