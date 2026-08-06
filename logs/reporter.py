"""
Daily/weekly summary report generator. Reads the signal journal SQLite DB and
produces a structured performance summary dict (and optionally a formatted string
for Telegram or stdout). Pure read-only — never writes to the journal.
"""
import sqlite3
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Optional


def _fetch_resolved(db_path: str, since_ts: Optional[str] = None) -> list:
    """Fetch all resolved (outcome not NULL) signal rows, optionally filtered by date."""
    query = "SELECT bias, confidence, session, regime, outcome, outcome_pnl FROM signal_journal WHERE outcome IS NOT NULL"
    params = []
    if since_ts:
        query += " AND generated_at >= ?"
        params.append(since_ts)
    with sqlite3.connect(db_path) as conn:
        return conn.execute(query, params).fetchall()


def generate_summary(db_path: str, since_ts: Optional[str] = None) -> dict:
    """
    Returns a performance summary dict over all resolved trades since since_ts.
    Keys: n_signals, n_resolved, win_rate, profit_factor, total_pnl, max_drawdown,
          by_session (dict of session -> {n, win_rate}), by_regime.
    """
    rows = _fetch_resolved(db_path, since_ts)
    if not rows:
        return {"n_resolved": 0, "win_rate": None, "profit_factor": None,
                "total_pnl": 0.0, "max_drawdown": 0.0, "by_session": {}, "by_regime": {}}

    pnls = [r[5] for r in rows if r[5] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) * 100 if pnls else None
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    cum = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum)
    drawdowns = cum - running_max
    max_drawdown = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0

    by_session = {}
    by_regime = {}
    for bias, conf, session, regime, outcome, pnl in rows:
        if pnl is None:
            continue
        for key, bucket in [(session, by_session), (regime, by_regime)]:
            if key not in bucket:
                bucket[key] = {"n": 0, "wins": 0, "total_pnl": 0.0}
            bucket[key]["n"] += 1
            bucket[key]["total_pnl"] += pnl
            if pnl > 0:
                bucket[key]["wins"] += 1

    for bucket in [by_session, by_regime]:
        for k in bucket:
            n = bucket[k]["n"]
            bucket[k]["win_rate"] = round(bucket[k]["wins"] / n * 100, 1) if n > 0 else None

    return {
        "n_resolved": len(pnls),
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
        "profit_factor": round(profit_factor, 3),
        "total_pnl": round(sum(pnls), 4),
        "max_drawdown": round(max_drawdown, 4),
        "by_session": by_session,
        "by_regime": by_regime,
    }


def format_summary_message(summary: dict) -> str:
    """Formats the summary dict into a human-readable string for Telegram or stdout."""
    if summary["n_resolved"] == 0:
        return "XAUUSD Performance Report\nNo resolved trades yet."

    lines = [
        "XAUUSD Performance Report",
        f"Resolved trades : {summary['n_resolved']}",
        f"Win rate        : {summary['win_rate']}%",
        f"Profit factor   : {summary['profit_factor']}",
        f"Total PnL       : {summary['total_pnl']}",
        f"Max drawdown    : {summary['max_drawdown']}",
    ]
    if summary["by_session"]:
        lines.append("\nBy session:")
        for s, m in summary["by_session"].items():
            lines.append(f"  {s}: {m['n']} trades, {m['win_rate']}% win rate, PnL {round(m['total_pnl'],4)}")
    return "\n".join(lines)
