"""Custom OnTester criterion (TZ_BOOKS task T-08; MQL5 book 6.5.6-6.5.11).

The MQL5 mirror lives in ``mql5/NeuroTrader/TesterCriterion.mqh``; this
module is the Python-side twin used by the backtest tooling so optimizer
rankings computed offline and inside the Strategy Tester agree bit-for-bit
on the formula:

    score = PF * sqrt(trades) - dd_weight * max_relative_drawdown(%)

The sqrt(trades) factor rewards statistical significance (the book's own
36-trade forward sample is thin - more trades at the same PF is better
evidence), while the drawdown penalty stops a high-PF-but-one-drawdown
curve from winning the optimization. Degenerate cases: PF = inf (no losing
trade) is capped at ``pf_cap``; zero trades score -inf so empty runs never
win an optimization pass.
"""
from __future__ import annotations

import math

DEFAULTS = {
    "dd_penalty_weight": 1.0,   # weight of the DD% penalty
    "pf_cap": 10.0,             # cap for infinite/absent-loss PF
    "min_trades": 1,            # below this the score is -inf
}


def on_tester_score(profit_factor: float, trades: int,
                    max_dd_percent: float, dd_penalty_weight: float | None = None,
                    pf_cap: float | None = None,
                    min_trades: int | None = None) -> float:
    """PF * sqrt(trades) - dd_penalty_weight * DD% (higher is better)."""
    p = DEFAULTS
    weight = p["dd_penalty_weight"] if dd_penalty_weight is None else dd_penalty_weight
    cap = p["pf_cap"] if pf_cap is None else pf_cap
    floor_trades = p["min_trades"] if min_trades is None else min_trades

    trades = int(trades or 0)
    if trades < max(1, floor_trades):
        return float("-inf")
    if profit_factor is None or (isinstance(profit_factor, float)
                                 and math.isnan(profit_factor)):
        return float("-inf")
    pf = float(profit_factor)
    if math.isinf(pf):
        pf = float(cap)
    pf = min(max(pf, 0.0), float(cap))
    dd = max(0.0, float(max_dd_percent or 0.0))
    return pf * math.sqrt(trades) - weight * dd


def score_from_tester_statistics(stats: dict, **kwargs) -> float:
    """Convenience wrapper taking a TesterStatistics-like dict (MQL5 book
    6.5.6): STAT_PROFIT_FACTOR, STAT_TRADES, STAT_EQUITY_DDRELATIVE."""
    return on_tester_score(
        profit_factor=stats.get("STAT_PROFIT_FACTOR", stats.get("profit_factor", 0.0)),
        trades=stats.get("STAT_TRADES", stats.get("trades", 0)),
        max_dd_percent=stats.get("STAT_EQUITY_DDRELATIVE",
                                 stats.get("max_dd_relative", 0.0)),
        **kwargs,
    )


def score_from_trades(trades_pnl: list[float], equity_curve: list[float] | None = None,
                      **kwargs) -> float:
    """Compute the criterion directly from a trade PnL list (+ optional
    equity curve for the relative drawdown)."""
    if not trades_pnl:
        return float("-inf")
    from backtest.validation_protocol import forward_metrics
    m = forward_metrics(trades_pnl)
    dd = 0.0
    if equity_curve:
        peak = float("-inf")
        for v in equity_curve:
            peak = max(peak, v)
            if peak > 0:
                dd = max(dd, (peak - v) / peak * 100.0)
    return on_tester_score(m["profit_factor"], m["trades"], dd, **kwargs)
