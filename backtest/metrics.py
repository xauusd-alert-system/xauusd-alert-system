"""
Backtest performance metrics: win rate, profit factor, max drawdown - computed
overall and split per trading session (Asia/London/NY).
"""
import numpy as np
import pandas as pd
from typing import List
from backtest.engine import Trade


def trades_to_dataframe(trades: List[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["entry_ts", "exit_ts", "direction", "session", "regime_at_entry", "pnl", "exit_reason"])
    return pd.DataFrame([{
        "entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "direction": t.direction,
        "session": t.session, "regime_at_entry": t.regime_at_entry,
        "pnl": t.pnl, "exit_reason": t.exit_reason,
    } for t in trades])


def compute_metrics(trades_df: pd.DataFrame) -> dict:
    """
    win_rate: % of trades with pnl > 0
    profit_factor: gross profit / gross loss (inf if no losses, NaN if no trades)
    max_drawdown: largest peak-to-trough decline in cumulative pnl equity curve
    """
    if len(trades_df) == 0:
        return {"n_trades": 0, "win_rate": np.nan, "profit_factor": np.nan, "max_drawdown": np.nan, "total_pnl": 0.0}

    wins = trades_df[trades_df["pnl"] > 0]
    losses = trades_df[trades_df["pnl"] <= 0]
    gross_profit = wins["pnl"].sum()
    gross_loss = -losses["pnl"].sum()

    win_rate = len(wins) / len(trades_df) * 100
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    cum_pnl = trades_df["pnl"].cumsum()
    running_max = cum_pnl.cummax()
    drawdown = cum_pnl - running_max
    max_drawdown = drawdown.min() if len(drawdown) > 0 else 0.0

    return {
        "n_trades": len(trades_df),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
        "total_pnl": trades_df["pnl"].sum(),
    }


def compute_metrics_per_session(trades_df: pd.DataFrame) -> dict:
    """
    Splits metrics by session tag. A trade's session is tagged at ENTRY time
    (open_position.session, set from the candle where entry executed) - consistent
    with how a live trader would categorize "which session did I take this trade in".
    """
    if len(trades_df) == 0:
        return {}
    results = {}
    for session_name, group in trades_df.groupby("session"):
        results[session_name] = compute_metrics(group)
    return results
