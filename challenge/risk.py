"""Challenge risk rules: half-of-platform-limit cushions and sizing.

Platform limits (Stage 1): daily loss -$50, total loss -$100, leverage 1:5,
target +$80. We trade to HALF of those stops so slippage on the platform's
auto-close can never breach the hard limits, and we lock the day's profit
early instead of giving it back.
"""


class ChallengeRisk:
    def __init__(self, cfg):
        r = cfg.get("risk", {})
        self.per_trade_risk_usd = float(r.get("per_trade_risk_usd", 5))
        self.daily_loss_stop = float(r.get("daily_loss_stop", 25))
        self.total_loss_stop = float(r.get("total_loss_stop", 60))
        self.daily_profit_lock = float(r.get("daily_profit_lock", 20))
        self.max_open_positions = int(r.get("max_open_positions", 2))
        self.max_leverage = float(r.get("max_leverage", 5))
        self.stop_pct = float(r.get("stop_pct", 0.005))
        self.tp_ratio = float(r.get("tp_ratio", 1.5))

    def position_size(self, price: float, equity: float) -> int:
        stop_dist = price * self.stop_pct
        if stop_dist <= 0 or price <= 0 or equity <= 0:
            return 0
        by_risk = self.per_trade_risk_usd / stop_dist
        by_leverage = equity * self.max_leverage / price
        return max(0, int(by_risk if by_risk < by_leverage else by_leverage))

    def stop_tp(self, price: float, bias: str):
        stop_dist = price * self.stop_pct
        if bias == "long":
            return price - stop_dist, price + stop_dist * self.tp_ratio
        return price + stop_dist, price - stop_dist * self.tp_ratio

    def evaluate(self, equity: float, day_start_equity: float,
                 total_start_equity: float):
        """Returns (action, reason) with action in
        {'trade', 'flatten_day', 'halt'}."""
        daily_pnl = equity - day_start_equity
        total_pnl = equity - total_start_equity
        if total_pnl <= -self.total_loss_stop:
            return "halt", f"total loss stop ({total_pnl:+.2f})"
        if daily_pnl <= -self.daily_loss_stop:
            return "flatten_day", f"daily loss stop ({daily_pnl:+.2f})"
        if daily_pnl >= self.daily_profit_lock:
            return "flatten_day", f"daily profit lock ({daily_pnl:+.2f})"
        return "trade", "ok"