"""Challenge risk rules with confirmed Hash Hedge static drawdown and floating equity.

Platform limits Stage 1: daily loss -$50 floating equity realtime, total loss -$100 static from start, leverage 1:5, target +$80.
Bot hard stops: daily -$30 floating (before platform auto-close), overall -$90 floating (buffer $10).
Daily reset 00:00-00:13 UTC+4.
All timing constants inside stealth modules, not here.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional


class ChallengeRisk:
    """Risk for UTEx challenge with floating equity tracking."""

    # Confirmed limits
    DAILY_LOSS_LIMIT = 50.0
    MAX_OVERALL_LOSS = 100.0
    DAILY_HARD_STOP = 30.0
    OVERALL_BUFFER = 90.0  # stop at -$90 floating
    LEVERAGE = 5.0
    BUYING_POWER_MULT = 5.0

    def __init__(self, cfg):
        r = cfg.get("risk", {})
        # Per-trade risk $10 base (1% of $1000) with jitter 0.7-1.3% handled in stealth
        self.per_trade_risk_usd = float(r.get("per_trade_risk_usd", 10.0))
        self.daily_loss_stop = float(r.get("daily_loss_stop", 30.0))  # bot hard stop -$30
        self.total_loss_stop = float(r.get("total_loss_stop", 90.0))  # overall buffer -$90
        self.daily_profit_lock = float(r.get("daily_profit_lock", 999999))  # consistency rule absent, no profit throttle
        self.max_open_positions = int(r.get("max_open_positions", 2))
        self.max_leverage = float(r.get("max_leverage", 5.0))
        self.stop_pct = float(r.get("stop_pct", 0.005))
        self.tp_ratio = float(r.get("tp_ratio", 2.0))  # 2R base

        # Stealth config overrides
        try:
            from config.loader import load_config
            full_cfg = load_config()
            stealth = full_cfg.get("stealth", {}) or {}
            self.daily_loss_stop = float(stealth.get("challenge_daily_hard_stop", 30.0))
            self.total_loss_stop = float(stealth.get("challenge_overall_buffer", 90.0) if stealth.get("challenge_overall_buffer", 90.0) > 50 else 90.0)
            # Actually overall buffer is 10, so limit is 90
            if stealth.get("challenge_overall_buffer") == 10.0:
                self.total_loss_stop = 90.0
        except Exception:
            pass

    def position_size(self, price: float, equity: float) -> int:
        """Shares = $risk / SL $ with buying power check."""
        stop_dist = price * self.stop_pct
        if stop_dist <= 0 or price <= 0 or equity <= 0:
            return 0
        by_risk = self.per_trade_risk_usd / stop_dist
        by_leverage = equity * self.max_leverage / price
        return max(0, int(by_risk if by_risk < by_leverage else by_leverage))

    def position_size_from_stop(self, entry: float, stop: float, risk_usd: float, equity: float) -> int:
        """Calculate shares from actual SL distance, check notional ≤ buying power."""
        sl_dist = abs(entry - stop)
        if sl_dist <= 0 or entry <= 0:
            return 1
        raw_shares = risk_usd / sl_dist
        shares = max(1, int(raw_shares))
        # Buying power check: notional = entry * shares ≤ equity * leverage
        buying_power = equity * self.max_leverage
        notional = entry * shares
        if notional > buying_power:
            shares = max(1, int(buying_power / entry))
        return shares

    def stop_tp(self, price: float, bias: str):
        stop_dist = price * self.stop_pct
        if bias == "long":
            return price - stop_dist, price + stop_dist * self.tp_ratio
        return price + stop_dist, price - stop_dist * self.tp_ratio

    def evaluate(self, equity: float, day_start_equity: float, total_start_equity: float, floating_pnl: float = 0, closed_pnl_today: float = 0):
        """Returns (action, reason) with action in {'trade', 'flatten_day', 'halt'}.

        Uses floating equity realtime: daily_loss = floating + closed_since_reset.
        """
        # Daily loss floating
        daily_pnl_floating = floating_pnl + closed_pnl_today
        overall_pnl_floating = equity - total_start_equity  # equity already includes floating

        # Overall buffer -$90 floating
        if overall_pnl_floating <= -90:
            return "halt", f"overall buffer hit {overall_pnl_floating:+.2f} <= -90 (limit -$90 floating, platform -$100)"
        if overall_pnl_floating <= -self.MAX_OVERALL_LOSS:
            return "halt", f"max overall loss {overall_pnl_floating:+.2f} <= -{self.MAX_OVERALL_LOSS}"

        # Daily hard stop -$30 floating (before platform -$50)
        if daily_pnl_floating <= -30:
            return "flatten_day", f"daily hard stop {daily_pnl_floating:+.2f} <= -30 floating (platform -$50)"
        if daily_pnl_floating <= -self.DAILY_LOSS_LIMIT:
            return "flatten_day", f"daily loss limit {daily_pnl_floating:+.2f} <= -{self.DAILY_LOSS_LIMIT}"

        # Consistency rule absent -> no profit lock
        return "trade", "ok"

    def evaluate_floating(self, daily_pnl_floating: float, overall_pnl_floating: float):
        """Direct floating PnL check for continuous monitor."""
        if overall_pnl_floating <= -90:
            return "halt", f"overall buffer {overall_pnl_floating:+.2f} <= -90"
        if daily_pnl_floating <= -30:
            return "flatten_day", f"daily hard stop {daily_pnl_floating:+.2f} <= -30"
        return "trade", "ok"
