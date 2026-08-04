"""
Advanced Institutional Backtest Performance Metrics:
Win Rate, Profit Factor, Sharpe Ratio, Sortino Ratio, Expectancy, Drawdown, Max Consec Loss.
"""
import numpy as np
import pandas as pd
from typing import List


def trades_to_dataframe(trades) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=["entry_ts", "exit_ts", "direction", "session", "regime_at_entry", "pnl", "exit_reason"]
        )
    return pd.DataFrame(
        [
            {
                "entry_ts": t.entry_ts,
                "exit_ts": t.exit_ts,
                "direction": t.direction,
                "session": t.session,
                "regime_at_entry": t.regime_at_entry,
                "pnl": t.pnl,
                "exit_reason": t.exit_reason,
            }
            for t in trades
        ]
    )


def compute_metrics(trades_df: pd.DataFrame) -> dict:
    if len(trades_df) == 0:
        return {
            "n_trades": 0,
            "win_rate": np.nan,
            "profit_factor": np.nan,
            "sharpe_ratio": np.nan,
            "sortino_ratio": np.nan,
            "expectancy": 0.0,
            "max_drawdown": 0.0,
            "total_pnl": 0.0,
            "max_consecutive_losses": 0,
        }

    pnls = trades_df["pnl"].values
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    gross_profit = wins.sum() if len(wins) > 0 else 0.0
    gross_loss = -losses.sum() if len(losses) > 0 else 0.0

    win_rate = (len(wins) / len(pnls)) * 100.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.inf

    # Expectancy ($ per trade)
    expectancy = pnls.mean()

    # Sharpe & Sortino (Annualized based on ~250 trading days)
    mean_pnl = pnls.mean()
    std_pnl = pnls.std()
    sharpe_ratio = (mean_pnl / std_pnl * np.sqrt(250)) if std_pnl > 0 else 0.0

    downside_pnls = pnls[pnls < 0]
    downside_std = downside_pnls.std() if len(downside_pnls) > 0 else 0.0
    sortino_ratio = (mean_pnl / downside_std * np.sqrt(250)) if downside_std > 0 else 0.0

    # Drawdown
    cum_pnl = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = cum_pnl - running_max
    max_drawdown = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0

    # Max Consecutive Losses
    max_consec_loss = 0
    current_consec = 0
    for pnl in pnls:
        if pnl <= 0:
            current_consec += 1
            if current_consec > max_consec_loss:
                max_consec_loss = current_consec
        else:
            current_consec = 0

    return {
        "n_trades": len(trades_df),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2) if not np.isinf(profit_factor) else 999.0,
        "sharpe_ratio": round(sharpe_ratio, 2),
        "sortino_ratio": round(sortino_ratio, 2),
        "expectancy": round(expectancy, 2),
        "max_drawdown": round(max_drawdown, 2),
        "total_pnl": round(float(cum_pnl[-1]), 2),
        "max_consecutive_losses": max_consec_loss,
    }


def compute_metrics_per_session(trades_df: pd.DataFrame) -> dict:
    """
    Compute full metrics broken down by session label.

    Returns a dict keyed by session name -> metrics dict (the output of
    compute_metrics() for that session's trades). Sessions with no trades are
    omitted entirely, so every returned entry has n_trades >= 1.
    """
    if trades_df is None or len(trades_df) == 0 or "session" not in trades_df.columns:
        return {}
    per_session: dict = {}
    for session_name, group in trades_df.groupby("session"):
        per_session[str(session_name)] = compute_metrics(group.reset_index(drop=True))
    return per_session