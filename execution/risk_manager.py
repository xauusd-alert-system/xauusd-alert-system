"""
Institutional Risk Manager & Circuit Breaker.
Protects deposit from over-leverage and daily drawdown limits.

Audit 2026-08-10 fixes:
  - W8: risk limits come from config `execution.*` instead of hard-coded 3/10.
  - W9: the concurrent-position check counts only THIS system's positions
        (filtered by `magic`), not foreign/manual MT5 positions.
  - W10: daily circuit-breaker state (starting equity, trade counters) is
        persisted to `logs/risk_state.json` so a process restart cannot reset
        the drawdown budget and re-arm trading after a losing day.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
import MetaTrader5 as mt5

logger = logging.getLogger("risk_manager")


class InstitutionalRiskManager:
    def __init__(self, cfg: dict, magic: int = None,
                 state_path: str = "logs/risk_state.json"):
        self.cfg = cfg
        self.magic = magic
        self.state_path = state_path

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

        # W10: restore persisted daily state (circuit-breaker budget) across
        # process restarts.
        self.current_day = datetime.now(timezone.utc).date()
        self.starting_equity_today = None
        self.daily_trades_count = {}
        self.circuit_breaker_tripped = False
        self._load_state()

    # ------------------------------------------------------------------ W10
    def _load_state(self):
        """Restore yesterday's/today's persisted risk state on startup."""
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning(f"Could not read risk state {self.state_path}; starting fresh.")
            return
        if not isinstance(data, dict):
            return
        try:
            self.current_day = datetime.fromisoformat(data["current_day"]).date()
            self.starting_equity_today = data.get("starting_equity_today")
            self.daily_trades_count = data.get("daily_trades_count", {})
            self.circuit_breaker_tripped = bool(data.get("circuit_breaker_tripped", False))
        except (KeyError, ValueError, TypeError):
            logger.warning(f"Malformed risk state {self.state_path}; starting fresh.")

    def _save_state(self):
        """Persist current daily state so a restart keeps the same budget."""
        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        data = {
            "current_day": self.current_day.isoformat(),
            "starting_equity_today": self.starting_equity_today,
            "daily_trades_count": self.daily_trades_count,
            "circuit_breaker_tripped": self.circuit_breaker_tripped,
        }
        tmp_path = self.state_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.state_path)
        except OSError as e:
            logger.error(f"Failed to persist risk state: {e}")

    def _reset_daily_stats_if_needed(self, current_equity: float):
        today = datetime.now(timezone.utc).date()
        if today != self.current_day or self.starting_equity_today is None:
            self.current_day = today
            self.starting_equity_today = current_equity
            self.daily_trades_count = {}
            self.circuit_breaker_tripped = False
            logger.info(f"🛡 Risk Manager Reset for {today}. Starting Daily Equity: ${current_equity:.2f}")
            self._save_state()

    def _positions(self):
        """This system's own open positions (filtered by magic when configured).

        W9: the real MT5 `positions_get` has no `magic` parameter (the test shim
        used to accept it, masking the bug), so the filter is applied in Python
        via `pos.magic`. Without the filter, foreign/manual MT5 positions would
        falsely consume our concurrency budget.
        """
        try:
            positions = mt5.positions_get() or []
        except Exception as e:  # pragma: no cover - defensive
            logger.error(f"positions_get failed: {e}")
            return []
        if self.magic is None:
            return list(positions)
        return [p for p in positions if getattr(p, "magic", None) == self.magic]

    def can_trade(self, asset_key: str, groups_by_asset: dict = None,
                  singles_by_asset: dict = None) -> tuple[bool, str]:
        """
        Validates whether a new trade is allowed under risk limits.

        groups_by_asset / singles_by_asset (audit 2026-08-19): the caller
        (mt5_trader) passes the OPEN positions grouped by identity: each
        3-leg group consumes ONE budget slot. When both are None (legacy
        callers/tests), every position consumes one slot (old behaviour).
        """
        if not mt5.initialize():
            return False, "MT5 not initialized"

        account_info = mt5.account_info()
        if not account_info:
            return False, "Could not fetch account info"

        current_equity = account_info.equity
        self._reset_daily_stats_if_needed(current_equity)

        # 1. 🚨 ПРОВЕРКА ДНЕВНОЙ ПРОСАДКИ (Circuit Breaker)
        max_allowed_loss = self.starting_equity_today * (self.max_daily_loss_pct / 100.0)
        current_daily_pnl = current_equity - self.starting_equity_today

        if current_daily_pnl <= -max_allowed_loss:
            self.circuit_breaker_tripped = True
            self._save_state()
            return False, f"🚨 CIRCUIT BREAKER TRIPPED! Daily loss (-${abs(current_daily_pnl):.2f}) exceeded limit (-${max_allowed_loss:.2f}). Trading halted for today."

        if self.circuit_breaker_tripped:
            return False, "Trading halted today by Circuit Breaker."

        # 2. 🚨 ПРОВЕРКА МАКСИМУМА ОДНОВРЕМЕННЫХ ГРУПП/ПОЗИЦИЙ
        # W9: only this system's positions (filtered by magic) count.
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
                return False, f"Max concurrent positions limit reached ({len(open_positions)}/{self.max_concurrent_positions})"

        # 3. 🚨 ПРОВЕРКА ДНЕВНОГО ЛИМИТА СДЕЛОК НА АКТИВ
        asset_trades = self.daily_trades_count.get(asset_key, 0)
        if asset_trades >= self.max_daily_trades_per_asset:
            return False, f"Daily trade limit for {asset_key} reached ({asset_trades}/{self.max_daily_trades_per_asset})"

        return True, "OK"

    def record_trade_executed(self, asset_key: str):
        self.daily_trades_count[asset_key] = self.daily_trades_count.get(asset_key, 0) + 1
        self._save_state()
