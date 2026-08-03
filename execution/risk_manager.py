"""
Institutional Risk Manager & Circuit Breaker.
Protects deposit from over-leverage and daily drawdown limits.
"""
import logging
import time
from datetime import datetime, timezone
import MetaTrader5 as mt5

logger = logging.getLogger("risk_manager")


class InstitutionalRiskManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.max_daily_loss_pct = cfg.get("backtest", {}).get("max_daily_loss_pct", 5.0)  # 5% макс убыток в день
        self.max_daily_trades_per_asset = 10
        self.max_concurrent_positions = 3

        self.current_day = datetime.now(timezone.utc).date()
        self.starting_equity_today = None
        self.daily_trades_count = {}
        self.circuit_breaker_tripped = False

    def _reset_daily_stats_if_needed(self, current_equity: float):
        today = datetime.now(timezone.utc).date()
        if today != self.current_day or self.starting_equity_today is None:
            self.current_day = today
            self.starting_equity_today = current_equity
            self.daily_trades_count = {}
            self.circuit_breaker_tripped = False
            logger.info(f"🛡 Risk Manager Reset for {today}. Starting Daily Equity: ${current_equity:.2f}")

    def can_trade(self, asset_key: str) -> tuple[bool, str]:
        """
        Validates whether a new trade is allowed under risk limits.
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
            return False, f"🚨 CIRCUIT BREAKER TRIPPED! Daily loss (-${abs(current_daily_pnl):.2f}) exceeded limit (-${max_allowed_loss:.2f}). Trading halted for today."

        if self.circuit_breaker_tripped:
            return False, "Trading halted today by Circuit Breaker."

        # 2. 🚨 ПРОВЕРКА МАКСИМУМА ОДНОВРЕМЕННЫХ ПОЗИЦИЙ
        open_positions = mt5.positions_get()
        if open_positions and len(open_positions) >= self.max_concurrent_positions:
            return False, f"Max concurrent positions limit reached ({len(open_positions)}/{self.max_concurrent_positions})"

        # 3. 🚨 ПРОВЕРКА ДНЕВНОГО ЛИМИТА СДЕЛОК НА АКТИВ
        asset_trades = self.daily_trades_count.get(asset_key, 0)
        if asset_trades >= self.max_daily_trades_per_asset:
            return False, f"Daily trade limit for {asset_key} reached ({asset_trades}/{self.max_daily_trades_per_asset})"

        return True, "OK"

    def record_trade_executed(self, asset_key: str):
        self.daily_trades_count[asset_key] = self.daily_trades_count.get(asset_key, 0) + 1