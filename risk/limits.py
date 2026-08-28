"""Daily / position limits and the circuit breaker (ТЗ 8.5 → moved from
``execution/risk_manager.py``).

Responsibility:
    The *limit* gates of the risk engine (one source of daily limits —
    P2-10):

    - daily loss circuit breaker with ``exclude_swaps`` (P0-5): the daily PnL
      is (balance − starting_balance_today) + (equity − balance) so an
      overnight swap settling into ``balance`` cannot trip the breaker on its
      own;
    - concurrent positions/groups (W8/W9: config-driven, magic-filtered,
      group-aware counting — a 3-leg group consumes ONE slot);
    - max open groups per asset;
    - max daily trades per asset.

    Pure *rate* throttling lives in ``risk/throttle.py`` (P2-10); sizing math
    lives in ``risk/sizing.py``.

Inputs / outputs:
    ``RiskLimits.can_trade(asset_key, groups_by_asset, singles_by_asset)``
    → ``(allowed: bool, reason: str)``. Position state comes either from the
    caller (grouped dicts, preferred — see execution/mt5_trader.py) or from
    MT5 ``positions_get`` filtered by ``magic`` (legacy path).

Dependencies:
    ``mt5_adapter.lazy.get_mt5_module`` (ТЗ 8.6 — no direct MetaTrader5
    import), ``risk.state.RiskState`` for persistence.

Example::

    limits = RiskLimits(cfg, magic=777111, state=RiskState("logs/risk_state.json"))
    ok, reason = limits.can_trade("XAUUSD", groups_by_asset={"XAUUSD": {"G1"}},
                                  singles_by_asset={})
"""
from __future__ import annotations

import logging
from typing import Optional

from mt5_adapter.lazy import get_mt5_module
from risk.state import RiskState

# ТЗ 8.6: raw module handle via the adapter (no direct `import MetaTrader5`).
mt5 = get_mt5_module()

logger = logging.getLogger("risk.limits")


class RiskLimits:
    """Daily loss / concurrency / daily-trade gates with a persistent
    circuit-breaker budget. Mirrors the historical ``InstitutionalRiskManager``
    behaviour exactly (W8/W9/W10 + P0-5)."""

    def __init__(self, cfg: dict, magic: Optional[int] = None,
                 state: Optional[RiskState] = None,
                 state_path: str = "logs/risk_state.json"):
        self.cfg = cfg
        self.magic = magic
        self.state = state if state is not None else RiskState(state_path)

        exec_cfg = cfg.get("execution", {})
        # W8: read the live concurrency/trade limits from config instead of the
        # hard-coded 3/10 that ignored `execution.*`.
        self.max_concurrent_positions = int(
            exec_cfg.get("max_concurrent_positions_global", 3)
        )
        # Audit 2026-08-19 (owner request): the global budget counts GROUPS
        # (3 legs of one signal = 1 slot), so N slots = N assets, not N/3
        # assets. The per-asset group cap (long unwired) is now enforced too.
        self.max_open_positions_per_asset = int(
            exec_cfg.get("max_open_positions_per_asset", 2)
        )
        self.max_daily_trades_per_asset = int(
            exec_cfg.get("max_daily_trades_per_asset", 10)
        )
        self.max_daily_loss_pct = float(
            cfg.get("backtest", {}).get("max_daily_loss_pct", 5.0)
        )

        # P0-5: the circuit breaker measures TRADING loss, excluding swaps and
        # other carry adjustments that MT5 settles into `balance` overnight.
        self.exclude_swaps = bool(
            cfg.get("risk", {}).get("circuit_breaker", {}).get(
                "exclude_swaps", True))

    # --------------------------------------------------- daily-budget gate
    def _reset_daily_stats_if_needed(self, current_equity: float,
                                     current_balance: Optional[float] = None) -> None:
        """Anchor/reset the daily budget when the UTC date rolls over."""
        st = self.state
        if not st.is_today() or st.starting_equity_today is None:
            st.reset_for_new_day(current_equity, current_balance)
            logger.info(
                "🛡 Risk Manager Reset for %s. Starting Daily Equity: $%.2f",
                st.current_day, current_equity)

    def daily_pnl(self, current_equity: float,
                  current_balance: float) -> float:
        """P0-5 daily PnL: trading loss only when ``exclude_swaps`` is set.

        exclude_swaps=True: (balance - starting_balance) + (equity - balance)
        — the swap part of the balance delta is not subtracted.
        exclude_swaps=False: legacy equity-delta behaviour (swaps count).
        """
        st = self.state
        if self.exclude_swaps:
            floating_pnl = current_equity - current_balance
            trading_balance_delta = current_balance - (
                st.starting_balance_today or 0.0)
            return trading_balance_delta + floating_pnl
        return current_equity - (st.starting_equity_today or 0.0)

    def check_circuit_breaker(self, current_equity: float,
                              current_balance: float) -> tuple[bool, str]:
        """Daily-loss circuit breaker. Persists the trip flag (W10)."""
        self._reset_daily_stats_if_needed(current_equity, current_balance)
        st = self.state

        max_allowed_loss = (st.starting_equity_today or 0.0) * (
            self.max_daily_loss_pct / 100.0)
        current_daily_pnl = self.daily_pnl(current_equity, current_balance)

        if current_daily_pnl <= -max_allowed_loss:
            st.circuit_breaker_tripped = True
            st.save()
            return False, (
                f"🚨 CIRCUIT BREAKER TRIPPED! Daily loss "
                f"(-${abs(current_daily_pnl):.2f}) exceeded limit "
                f"(-${max_allowed_loss:.2f}). Trading halted for today.")

        if st.circuit_breaker_tripped:
            return False, "Trading halted today by Circuit Breaker."

        return True, "OK"

    # -------------------------------------------------------- MT5 position
    def _positions(self) -> list:
        """This system's own open positions (filtered by magic when set).

        W9: the real MT5 `positions_get` has no `magic` parameter, so the
        filter is applied in Python via `pos.magic`. Without the filter,
        foreign/manual MT5 positions would falsely consume our concurrency
        budget.
        """
        try:
            positions = mt5.positions_get() or []
        except Exception as e:  # pragma: no cover - defensive
            logger.error(f"positions_get failed: {e}")
            return []
        if self.magic is None:
            return list(positions)
        return [p for p in positions if getattr(p, "magic", None) == self.magic]

    # ------------------------------------------------------------ gates
    def check_concurrency(self, asset_key: str,
                          groups_by_asset: Optional[dict] = None,
                          singles_by_asset: Optional[dict] = None) -> tuple[bool, str]:
        """Concurrent groups/positions caps (group-aware; legacy raw-count
        fallback when no grouping info is supplied)."""
        if groups_by_asset is not None and singles_by_asset is not None:
            total_groups = (
                sum(len(keys) for keys in groups_by_asset.values())
                + sum(singles_by_asset.values())
            )
            if total_groups >= self.max_concurrent_positions:
                return False, (
                    f"Max concurrent groups limit reached "
                    f"({total_groups}/{self.max_concurrent_positions})")
            asset_groups = (
                len(groups_by_asset.get(asset_key, ()))
                + singles_by_asset.get(asset_key, 0)
            )
            if asset_groups >= self.max_open_positions_per_asset:
                return False, (
                    f"Max open groups for {asset_key} reached "
                    f"({asset_groups}/{self.max_open_positions_per_asset})")
        else:
            open_positions = self._positions()
            if open_positions and len(open_positions) >= self.max_concurrent_positions:
                return False, (
                    f"Max concurrent positions limit reached "
                    f"({len(open_positions)}/{self.max_concurrent_positions})")
        return True, "OK"

    def check_daily_trades(self, asset_key: str) -> tuple[bool, str]:
        """Daily executed-trade cap per asset (P2-10: THE one daily-limit
        source — the throttle does not duplicate it)."""
        asset_trades = self.state.daily_trades_count.get(asset_key, 0)
        if asset_trades >= self.max_daily_trades_per_asset:
            return False, (
                f"Daily trade limit for {asset_key} reached "
                f"({asset_trades}/{self.max_daily_trades_per_asset})")
        return True, "OK"

    def can_trade(self, asset_key: str, groups_by_asset: Optional[dict] = None,
                  singles_by_asset: Optional[dict] = None) -> tuple[bool, str]:
        """Aggregate the limit gates; the circuit breaker MUST be evaluated
        first (it anchors the daily budget as a side effect of the reset)."""
        if not mt5.initialize():
            return False, "MT5 not initialized"

        account_info = mt5.account_info()
        if not account_info:
            return False, "Could not fetch account info"

        current_equity = account_info.equity
        current_balance = account_info.balance

        ok, reason = self.check_circuit_breaker(
            current_equity, current_balance)
        if not ok:
            return False, reason

        ok, reason = self.check_concurrency(
            asset_key, groups_by_asset, singles_by_asset)
        if not ok:
            return False, reason

        return self.check_daily_trades(asset_key)

    def record_trade_executed(self, asset_key: str) -> None:
        """Count an executed trade and persist (W10)."""
        self.state.record_trade(asset_key)
        self.state.save()
