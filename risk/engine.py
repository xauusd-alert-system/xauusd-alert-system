"""RiskEngine — the single entry point for all risk gates (ТЗ 8.5).

Responsibility:
    Aggregate every pre-trade gate behind one call:

        engine.can_open(asset_key, ...) -> (allowed: bool, reason: str)

    Gates, in evaluation order (first failure wins, reason strings keep the
    historical wording so logs/tests stay compatible):

    1. Daily circuit breaker          (risk/limits.py — P0-5 exclude_swaps,
                                       persistent budget in risk_state.json)
    2. Concurrency / per-asset caps   (risk/limits.py — group-aware, magic
                                       filtered)
    3. Daily trades per asset         (risk/limits.py — single daily-limit
                                       source, P2-10)
    4. Rate throttle (N orders/min)   (risk/throttle.py — frequency only,
                                       P2-10)
    5. Drawdown throttle vs HWM       (P1-7 — HWM is crossing/persistent in
                                       risk/state.py; -8% => no new entries)
    6. Cluster exposure caps          (P1-4 — cluster_cap/total_cap REQUIRED)
    7. Optional legacy TradeThrottle  (deprecated execution shim: loss-streak
                                       cooldown / hard stop; integrated until
                                       the old files are deleted)

    ``can_trade(...)`` is a backwards-compatible alias for ``can_open`` (the
    historical public API name).

Inputs / outputs:
    See ``can_open`` for the accepted kwargs. Nothing here writes to MT5;
    account/position reads go through ``risk/limits.py``.

Dependencies:
    risk.limits, risk.sizing, risk.state, risk.throttle; optionally
    execution.trade_throttle (legacy, injected or created from cfg).

Example::

    engine = RiskEngine(cfg, magic=777111)
    ok, reason = engine.can_open("XAUUSD", equity=10_000, balance=10_000,
                                 add_risk_pct=0.002, cluster="metals",
                                 cluster_cap=0.004, total_cap=0.0075)
    if not ok:
        logger.warning("blocked: %s", reason)
"""
from __future__ import annotations

import logging
from typing import Optional

from risk.limits import RiskLimits
from risk.sizing import cluster_exposure_ok, drawdown_throttle
from risk.state import RiskState
from risk.throttle import RateThrottle

logger = logging.getLogger("risk.engine")

# P1-7: drawdown vs HWM at which new live entries stop (shadow only).
_NO_ENTRY_DD = -0.08


class RiskEngine:
    """Facade aggregating all risk gates behind ``can_open``/``can_trade``."""

    def __init__(self, cfg: dict, magic: int = None,
                 state: Optional[RiskState] = None,
                 state_path: str = "logs/risk_state.json",
                 rate_throttle: Optional[RateThrottle] = None,
                 legacy_throttle=None):
        self.cfg = cfg
        self.state = state if state is not None else RiskState(state_path)
        self.limits = RiskLimits(cfg, magic=magic, state=self.state)
        self.rate_throttle = rate_throttle if rate_throttle is not None \
            else RateThrottle(cfg)
        # Legacy loss-streak throttle (deprecated execution shim). Optional:
        # pass None explicitly to run the engine without it (pure unit tests).
        if legacy_throttle is None:
            try:  # pragma: no cover - exercised via integration paths
                from execution.trade_throttle import TradeThrottle
                legacy_throttle = TradeThrottle(cfg)
            except Exception:
                legacy_throttle = None
        self.legacy_throttle = legacy_throttle

    # -------------------------------------------------------------- gates
    def _check_drawdown_throttle(self, equity: float) -> tuple[bool, str]:
        """P1-7: measure the CURRENT drawdown against the persistent HWM.

        The HWM ratchets upward only (never resets on a calendar-day change),
        so a throttle level stays engaged until equity makes a new high.
        """
        if equity <= 0 or self.state.hwm is None:
            return True, "OK"
        self.state.update_hwm(equity)
        dd = equity / self.state.hwm - 1.0
        mult = drawdown_throttle(dd)
        if mult <= 0.0 or dd <= _NO_ENTRY_DD:
            return False, (
                f"drawdown_throttle: DD from HWM {dd:.2%} -> no new entries "
                f"(risk multiplier {mult:.2f})")
        return True, "OK"

    def _check_cluster_exposure(self, cluster: str, add_risk_pct: float,
                                current_risk_by_cluster: Optional[dict],
                                cluster_cap: float,
                                total_cap: float) -> tuple[bool, str]:
        """P1-4: cluster/total stop-risk caps with REQUIRED cap parameters."""
        if add_risk_pct is None:
            return True, "OK"
        if cluster_cap is None or total_cap is None:
            raise TypeError(
                "cluster_cap and total_cap are required (P1-4: no defaults)")
        if current_risk_by_cluster is None:
            current_risk_by_cluster = {}
        check = cluster_exposure_ok(
            current_risk_by_cluster, cluster, add_risk_pct,
            cluster_cap=cluster_cap, total_cap=total_cap)
        if not check["ok"]:
            return False, (
                f"cluster_exposure_exceeded: cluster {cluster} would reach "
                f"{check['cluster_sum']:.4f} (cap {check['cluster_cap']:.4f}), "
                f"total {check['total_sum']:.4f} (cap {check['total_cap']:.4f})")
        return True, "OK"

    # ------------------------------------------------------------- public
    def can_open(self, asset_key: str, equity: float = 0.0,
                 balance: float = None, side: str = None,
                 groups_by_asset: dict = None, singles_by_asset: dict = None,
                 current_risk_by_cluster: dict = None, cluster: str = None,
                 add_risk_pct: float = None,
                 cluster_cap: float = None, total_cap: float = None,
                 use_legacy_throttle: bool = True) -> tuple[bool, str]:
        """Single pre-trade gate. Returns ``(allowed, reason)``.

        Gates are evaluated in the documented order; any gate returning False
        short-circuits with its reason string.

        Parameters:
            equity / balance          — account snapshots (circuit breaker +
                                        HWM); when equity <= 0 the MT5 account
                                        is read via risk/limits (legacy path).
            groups_by_asset/singles   — open-position grouping (see limits).
            cluster + add_risk_pct    — exposure caps check (P1-4); caps are
                                        REQUIRED when add_risk_pct is given.
            current_risk_by_cluster   — live stop-risk per cluster.
            use_legacy_throttle       — consult the deprecated loss-streak
                                        throttle when present (off in tests).
        """
        # Optional legacy throttle (cooldown / hard stop / its own daily-loss
        # gate). Kept first so a halted legacy state wins over everything.
        if use_legacy_throttle and self.legacy_throttle is not None:
            ok, reason = self.legacy_throttle.can_trade(equity or 0.0)
            if not ok:
                return False, f"throttle: {reason}"

        # 1-3. Circuit breaker + concurrency + daily trades (risk/limits.py).
        # When the caller supplies equity/balance we run the breaker check
        # directly; otherwise limits.can_trade reads MT5 itself.
        if equity and equity > 0:
            account_balance = balance if balance is not None else equity
            ok, reason = self.limits.check_circuit_breaker(
                equity, account_balance)
            if not ok:
                return False, reason
            ok, reason = self.limits.check_concurrency(
                asset_key, groups_by_asset, singles_by_asset)
            if not ok:
                return False, reason
            ok, reason = self.limits.check_daily_trades(asset_key)
            if not ok:
                return False, reason
        else:
            ok, reason = self.limits.can_trade(
                asset_key, groups_by_asset, singles_by_asset)
            if not ok:
                return False, reason

        # 4. Rate throttle (P2-10: frequency only).
        ok, reason = self.rate_throttle.can_trade(asset_key)
        if not ok:
            return False, reason

        # 5. Drawdown throttle vs persistent HWM (P1-7).
        if equity and equity > 0:
            ok, reason = self._check_drawdown_throttle(equity)
            if not ok:
                return False, reason

        # 6. Cluster exposure caps (P1-4: caps required).
        if add_risk_pct is not None:
            ok, reason = self._check_cluster_exposure(
                cluster or asset_key, add_risk_pct, current_risk_by_cluster,
                cluster_cap, total_cap)
            if not ok:
                return False, reason

        return True, "OK"

    # Backwards-compatible alias: `can_trade` was the historical public API
    # name (InstitutionalRiskManager.can_trade).
    def can_trade(self, asset_key: str, **kwargs) -> tuple[bool, str]:
        """Alias for :meth:`can_open` (historical API name)."""
        return self.can_open(asset_key, **kwargs)

    # ----------------------------------------------------------- recording
    def record_trade(self, asset_key: str, equity: float = None) -> None:
        """Record an executed trade: daily counter + persist (HWM ratchet
        happens on the next can_open with fresh equity)."""
        self.limits.record_trade_executed(asset_key)
        if equity is not None and equity > 0:
            self.state.update_hwm(equity)
        self.state.save()

    def record_order(self, asset_key: str) -> None:
        """Stamp the rate window for an order sent to the broker."""
        self.rate_throttle.record_order(asset_key)

    def summary(self) -> dict:
        """Diagnostic snapshot of the aggregated risk state."""
        return {
            "circuit_breaker_tripped": self.state.circuit_breaker_tripped,
            "current_day": self.state.current_day.isoformat()
            if self.state.current_day else None,
            "starting_equity_today": self.state.starting_equity_today,
            "starting_balance_today": self.state.starting_balance_today,
            "hwm": self.state.hwm,
            "daily_trades_count": dict(self.state.daily_trades_count),
            "rate_orders_per_asset": {
                k: len(v) for k, v in self.rate_throttle._orders.items()},
        }
