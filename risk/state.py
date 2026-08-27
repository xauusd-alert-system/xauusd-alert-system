"""Risk state persistence (ТЗ 8.5, P1-7).

Responsibility:
    Single owner of the persisted risk-state file (default
    ``logs/risk_state.json``). It stores everything that must survive a
    process restart:

    - ``current_day``                      — UTC date the budget belongs to;
    - ``starting_equity_today``            — circuit-breaker equity anchor;
    - ``starting_balance_today``           — circuit-breaker balance anchor
                                             (P0-5; absent in legacy files);
    - ``hwm``                              — equity high-water mark for the
                                             drawdown throttle (P1-7);
    - ``daily_trades_count``               — per-asset executed-trade counters;
    - ``circuit_breaker_tripped``          — daily halt flag.

Backwards compatibility:
    - Reads pre-P0-5 files that lack ``starting_balance_today`` (falls back to
      ``starting_equity_today``) and pre-P1-7 files that lack ``hwm``.
    - The JSON key names are unchanged — old files load as-is and new fields
      are simply missing until first save.

Inputs / outputs:
    ``RiskState(path)`` → ``load()`` fills the public attributes; ``save()``
    writes atomically (tmp file + ``os.replace``).

Dependencies:
    stdlib only (json, os, datetime).

Example::

    st = RiskState("logs/risk_state.json")
    st.load()
    if st.current_day != date.today():
        st.reset_for_new_day(equity=10_000.0)
    st.hwm = max(st.hwm or 0.0, equity)
    st.save()
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risk.state")


class RiskState:
    """Persistent container for daily risk state (see module docstring)."""

    def __init__(self, state_path: str = "logs/risk_state.json"):
        self.state_path = state_path

        # --- persisted attributes (all have safe defaults) -----------------
        self.current_day: Optional[date] = None
        self.starting_equity_today: Optional[float] = None
        self.starting_balance_today: Optional[float] = None
        self.hwm: Optional[float] = None
        self.daily_trades_count: dict[str, int] = {}
        self.circuit_breaker_tripped: bool = False

        self.load()

    # ------------------------------------------------------------------ I/O
    def load(self) -> None:
        """Restore state from disk; tolerate legacy/malformed files.

        Legacy formats accepted:
            - no ``starting_balance_today`` (pre-P0-5) -> falls back to
              ``starting_equity_today``;
            - no ``hwm`` (pre-P1-7) -> stays ``None`` until first update.
        """
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Could not read risk state %s; starting fresh.", self.state_path)
            return
        if not isinstance(data, dict):
            return
        try:
            if data.get("current_day"):
                self.current_day = datetime.fromisoformat(
                    data["current_day"]).date()
            self.starting_equity_today = data.get("starting_equity_today")
            # P0-5 backwards compat: old files lack the balance anchor.
            self.starting_balance_today = data.get(
                "starting_balance_today", self.starting_equity_today)
            self.hwm = data.get("hwm")
            self.daily_trades_count = dict(data.get("daily_trades_count", {}))
            self.circuit_breaker_tripped = bool(
                data.get("circuit_breaker_tripped", False))
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "Malformed risk state %s; starting fresh.", self.state_path)

    def save(self) -> None:
        """Persist state atomically (tmp + os.replace)."""
        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        data: dict[str, Any] = {
            "current_day": self.current_day.isoformat() if self.current_day else None,
            "starting_equity_today": self.starting_equity_today,
            "starting_balance_today": self.starting_balance_today,
            "hwm": self.hwm,
            "daily_trades_count": self.daily_trades_count,
            "circuit_breaker_tripped": self.circuit_breaker_tripped,
        }
        tmp_path = self.state_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.state_path)
        except OSError as e:
            logger.error("Failed to persist risk state: %s", e)

    # ------------------------------------------------------------- mutators
    def reset_for_new_day(self, current_equity: float,
                          current_balance: float = None) -> None:
        """Anchor a fresh daily budget (UTC date changed or first anchor).

        ``current_balance`` defaults to the equity value for legacy callers
        that cannot supply a balance (P0-5 note in risk/limits.py).
        """
        self.current_day = datetime.now(timezone.utc).date()
        self.starting_equity_today = current_equity
        self.starting_balance_today = (
            current_equity if current_balance is None else current_balance)
        self.daily_trades_count = {}
        self.circuit_breaker_tripped = False
        self.save()

    def update_hwm(self, equity: float) -> float:
        """Ratchet the equity high-water mark upward; returns the new HWM.

        P1-7: the HWM is *crossing* (persistent across days) — a drawdown
        throttle measured against it only recovers when new equity highs are
        made, not on a calendar day change.
        """
        equity = float(equity)
        if self.hwm is None or equity > self.hwm:
            self.hwm = equity
        return self.hwm

    def record_trade(self, asset_key: str) -> int:
        """Increment the per-asset daily trade counter; returns new value."""
        self.daily_trades_count[asset_key] = (
            self.daily_trades_count.get(asset_key, 0) + 1)
        return self.daily_trades_count[asset_key]

    def is_today(self) -> bool:
        """True when the persisted budget belongs to today (UTC)."""
        return self.current_day == datetime.now(timezone.utc).date()
