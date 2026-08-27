"""Model acceptance / validation protocol (TZ_BOOKS task T-03; NN book ch. 7
pages 651-690, MQL5 book 6.5.1 + 6.5.6).

The book's honest-validation regimen, fixed as code so no model reaches the
live path without passing it:

1. **Data split**: train / valid / test = 60 / 20 / 20, strictly
   time-ordered (no shuffling across boundaries) - see
   ``model/sample_generator.py``.
2. **Forward test**: the frozen model must survive >= 1 year of out-of-sample
   data on **real ticks** in the Strategy Tester (every-tick-based-on-real-
   ticks mode, MQL5 book table 6.5.1) - parameters stay frozen, nothing is
   re-fit on the forward window.
3. **Acceptance thresholds** (book forward result PF 1.48 / 58.33% win rate
   at TradeLevel 0.6 - the bar to beat):
     * forward Profit Factor > 1.2
     * forward win rate    > 55%
     * signal threshold    >= 0.6 (TradeLevel analog)
     * minimum trade count for the numbers to mean anything (default 30,
       book's 36-trade forward sample is the floor of statistical respect).

``evaluate_model_acceptance`` turns a metrics dict into an explicit
accepted/rejected decision with reasons, suitable for the deploy gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_PROTOCOL: dict = {
    "split_ratios": (0.6, 0.2, 0.2),
    "forward_min_days": 365,
    "tick_mode": "real_ticks",          # "Every tick based on real ticks"
    "min_profit_factor": 1.2,
    "min_win_rate": 0.55,
    "min_signal_threshold": 0.6,
    "min_trades": 30,
    "require_frozen_params": True,
}


@dataclass
class AcceptanceDecision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.accepted


def forward_metrics(trades_pnl: list[float]) -> dict:
    """Profit factor / win rate / trade count from a forward-test trade list."""
    if not trades_pnl:
        return {"trades": 0, "profit_factor": 0.0, "win_rate": 0.0,
                "gross_profit": 0.0, "gross_loss": 0.0, "net": 0.0}
    gross_profit = float(sum(p for p in trades_pnl if p > 0))
    gross_loss = float(-sum(p for p in trades_pnl if p < 0))
    wins = sum(1 for p in trades_pnl if p > 0)
    pf = gross_profit / gross_loss if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0)
    return {
        "trades": len(trades_pnl),
        "profit_factor": pf,
        "win_rate": wins / len(trades_pnl),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net": float(sum(trades_pnl)),
    }


def evaluate_model_acceptance(metrics: dict, protocol: dict | None = None,
                              signal_threshold_used: float | None = None,
                              forward_days: float | None = None,
                              params_frozen: bool | None = None) -> AcceptanceDecision:
    """Apply the T-03 acceptance gate to a forward-test metrics dict.

    ``metrics`` keys (see :func:`forward_metrics`): trades, profit_factor,
    win_rate. Extra protocol context is passed explicitly so the caller
    cannot silently claim compliance.
    """
    p = {**DEFAULT_PROTOCOL, **(protocol or {})}
    reasons: list[str] = []
    checks: dict = {}

    trades = int(metrics.get("trades", 0) or 0)
    pf = float(metrics.get("profit_factor", 0.0) or 0.0)
    wr = float(metrics.get("win_rate", 0.0) or 0.0)

    checks["min_trades"] = trades >= int(p["min_trades"])
    if not checks["min_trades"]:
        reasons.append(f"trades {trades} < {p['min_trades']} (book forward sample had 36; "
                       "smaller samples cannot support the PF/win-rate thresholds)")

    checks["profit_factor"] = pf > float(p["min_profit_factor"])
    if not checks["profit_factor"]:
        reasons.append(f"forward profit factor {pf:.3f} <= {p['min_profit_factor']}")

    checks["win_rate"] = wr > float(p["min_win_rate"])
    if not checks["win_rate"]:
        reasons.append(f"forward win rate {wr:.3f} <= {p['min_win_rate']}")

    if signal_threshold_used is not None:
        checks["signal_threshold"] = signal_threshold_used >= float(p["min_signal_threshold"])
        if not checks["signal_threshold"]:
            reasons.append(f"signal threshold {signal_threshold_used} < "
                           f"{p['min_signal_threshold']} (TradeLevel floor)")

    if forward_days is not None:
        checks["forward_length"] = forward_days >= float(p["forward_min_days"])
        if not checks["forward_length"]:
            reasons.append(f"forward window {forward_days:.0f} days < "
                           f"{p['forward_min_days']} required")

    if params_frozen is not None and p["require_frozen_params"]:
        checks["params_frozen"] = bool(params_frozen)
        if not checks["params_frozen"]:
            reasons.append("parameters were re-fit inside the forward window "
                           "(protocol violation: freeze then forward-test)")

    accepted = all(checks.values())
    if accepted:
        reasons.append(f"forward PF {pf:.2f} > {p['min_profit_factor']}, win rate "
                       f"{wr:.1%} > {float(p['min_win_rate']):.0%}, {trades} trades - "
                       "accepted (book reference: PF 1.48 / 58.33% / 36 trades)")
    return AcceptanceDecision(accepted=accepted, reasons=reasons, checks=checks)


def protocol_checklist() -> list[str]:
    """Human-readable checklist of the mandated validation regimen."""
    return [
        "1. Split the history time-ordered 60/20/20 (train/valid/test); never "
        "shuffle across the boundaries.",
        "2. Train on the train slice only; pick hyperparameters using the valid "
        "slice; freeze everything afterwards.",
        "3. Backtest the frozen model on the test slice, then run a forward "
        "window of >= 365 days in the Strategy Tester with 'Every tick based "
        "on real ticks' (MQL5 book 6.5.1) with costs (spread/slippage) on.",
        "4. Compute forward metrics (PF, win rate, trade count, max consecutive "
        "losses) and pass them through evaluate_model_acceptance().",
        "5. Only a decision with accepted=True may enter the live path; record "
        "the metrics and reasons alongside the model bundle.",
    ]
