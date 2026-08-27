"""Signal threshold (TradeLevel analog) + day-of-week filter (task T-10).

From the book's forward-test statistics (NN book pages 688-689): trading
only when the model's probability of direction is >= TradeLevel (0.6), and
skipping the week days that the instrument's OWN statistics show as losing
(Tuesday/Thursday for EURUSD in the book - XAUUSD must be measured on its
own history, never copied), converted losing days into "no trades" and was
called a direct source of extra profit.

Everything here is pure + fail-open: the filter is OFF unless explicitly
enabled in config, and unknown timestamps never block a signal by accident.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULT_TRADE_LEVEL = 0.6  # book p. 688

WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday",
                 "saturday", "sunday"]


def passes_trade_level(probability: float, trade_level: float = DEFAULT_TRADE_LEVEL) -> bool:
    """True when the directional probability clears the TradeLevel bar."""
    if not 0.5 < trade_level < 1.0:
        raise ValueError(f"trade_level must lie in (0.5, 1.0), got {trade_level}")
    return float(probability) >= trade_level


@dataclass
class DayStats:
    weekday: int
    trades: int
    wins: int
    win_rate: float
    total_pnl: float
    profit_factor: float


def day_of_week_stats(trades_df: pd.DataFrame, min_trades: int = 1) -> list[DayStats]:
    """Per-weekday performance stats from a trades frame.

    ``trades_df`` needs a timezone-naive-or-aware ``time`` column (entry
    time) and a ``pnl`` column; rows with missing values are dropped.
    """
    df = trades_df.copy()
    if "time" in df.columns:
        ts = pd.to_datetime(df["time"])
    elif "entry_time" in df.columns:
        ts = pd.to_datetime(df["entry_time"])
    else:
        raise ValueError("trades_df needs a 'time' (or 'entry_time') column")
    if "pnl" not in df.columns:
        raise ValueError("trades_df needs a 'pnl' column")
    df["_weekday"] = ts.dt.weekday
    df["_pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    df = df.dropna(subset=["_pnl"])

    out: list[DayStats] = []
    for wd in range(7):
        sub = df[df["_weekday"] == wd]
        trades = len(sub)
        if trades == 0:
            out.append(DayStats(wd, 0, 0, 0.0, 0.0, 0.0))
            continue
        wins = int((sub["_pnl"] > 0).sum())
        gross_profit = float(sub.loc[sub["_pnl"] > 0, "_pnl"].sum())
        gross_loss = float(-sub.loc[sub["_pnl"] < 0, "_pnl"].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else (
            float("inf") if gross_profit > 0 else 0.0)
        out.append(DayStats(
            weekday=wd, trades=trades, wins=wins, win_rate=wins / trades,
            total_pnl=float(sub["_pnl"].sum()), profit_factor=pf,
        ))
    return out


def blocked_days_from_stats(stats: list[DayStats], min_trades: int = 30,
                            max_win_rate: float = 0.45,
                            require_negative_pnl: bool = True) -> list[int]:
    """Suggest blocked weekdays: enough history AND (weak win rate or losing
    PnL). Defaults require 30+ trades on the day before blocking it - the
    book's own forward sample had 36 trades in TOTAL, so under-sampled days
    must never be blocked."""
    blocked = []
    for s in stats:
        if s.trades < min_trades:
            continue
        weak = s.win_rate < max_win_rate
        losing = (s.total_pnl < 0.0) if require_negative_pnl else False
        if weak and (losing or not require_negative_pnl):
            blocked.append(s.weekday)
    return blocked


def weekday_of(ts) -> int:
    """Weekday index (Mon=0) of any timestamp-like value."""
    if isinstance(ts, pd.Timestamp):
        return int(ts.weekday())
    ts = pd.Timestamp(ts)
    return int(ts.weekday())


def is_day_allowed(ts, blocked_days: list[int] | tuple[int, ...]) -> bool:
    return weekday_of(ts) not in set(blocked_days or ())


def apply_day_filter(df: pd.DataFrame, time_column: str,
                     blocked_days: list[int]) -> pd.DataFrame:
    """Filter a signals/trades frame to allowed weekdays only."""
    mask = pd.to_datetime(df[time_column]).dt.weekday.apply(
        lambda wd: wd not in set(blocked_days or ()))
    return df[mask].copy()


def filter_config(cfg: dict) -> dict:
    """Effective day-filter config from the ``books.day_of_week_filter``
    section with safe defaults (disabled)."""
    books = (cfg or {}).get("books", {}) or {}
    f = dict(books.get("day_of_week_filter", {}) or {})
    f.setdefault("enabled", False)
    f.setdefault("blocked_days", [])
    return f


def trade_level_config(cfg: dict) -> float:
    """Effective TradeLevel from ``books.trade_level`` (default 0.6)."""
    books = (cfg or {}).get("books", {}) or {}
    return float(books.get("trade_level", DEFAULT_TRADE_LEVEL))
