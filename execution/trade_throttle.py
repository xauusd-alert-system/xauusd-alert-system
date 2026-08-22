"""
TradeThrottle — daily trade limit + loss-streak cooldown + risk step-down.

Implements the "2.1 trading discipline" rules:
  1. Max trades per day (global or per-asset).
  2. Cooldown after consecutive losses (soft stop).
  3. Hard stop at critical loss streak (full halt until next session).
  4. Dynamic risk reduction (risk step-down multiplier).
  5. Daily loss limit (equity-based circuit breaker).

All parameters come from config `risk_throttle:` — no hard-coded values.
State persists to JSON so a process restart preserves the budget.
Thread-safe via a lock for concurrent callers.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("trade_throttle")

# ---------------------------------------------------------------------------
# Defaults (used when config key is missing)
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "max_trades_per_day": 5,
    "loss_streak_threshold": 2,
    "cooldown_minutes": 45,
    "hard_stop_streak": 3,
    "risk_step_down_map": {1: 1.0, 2: 0.5, 3: 0.25},
    "max_daily_loss_pct": 3.0,
    "reset_on_utc_midnight": True,
}


class TradeThrottle:
    """Gatekeeper that decides whether a new trade is allowed and what risk
    multiplier to apply.

    Usage::

        throttle = TradeThrottle(cfg)

        allowed, reason = throttle.can_trade("XAUUSD")
        if not allowed:
            logger.warning(f"Blocked: {reason}")
            return

        risk_mult = throttle.risk_multiplier()
        adjusted_lot = base_lot * risk_mult

        # ... after trade closes ...
        throttle.on_trade_closed(pnl_usd)
    """

    def __init__(self, cfg: dict, state_path: str = "logs/risk_throttle_state.json"):
        tc = (cfg or {}).get("risk_throttle", {}) or {}
        self.max_trades_per_day = int(tc.get("max_trades_per_day", _DEFAULTS["max_trades_per_day"]))
        self.loss_streak_threshold = int(tc.get("loss_streak_threshold", _DEFAULTS["loss_streak_threshold"]))
        self.cooldown_minutes = int(tc.get("cooldown_minutes", _DEFAULTS["cooldown_minutes"]))
        self.hard_stop_streak = int(tc.get("hard_stop_streak", _DEFAULTS["hard_stop_streak"]))
        self.risk_step_down_map: dict[int, float] = tc.get("risk_step_down_map") or _DEFAULTS["risk_step_down_map"]
        self.max_daily_loss_pct = float(tc.get("max_daily_loss_pct", _DEFAULTS["max_daily_loss_pct"]))
        self.reset_on_utc_midnight = bool(tc.get("reset_on_utc_midnight", _DEFAULTS["reset_on_utc_midnight"]))

        self.state_path = state_path
        self._lock = threading.RLock()  # reentrant: get_state() calls risk_multiplier() which also acquires

        # --- mutable state ---
        self.current_day: datetime.date | None = None
        self.starting_equity: float = 0.0
        self.trades_today: int = 0
        self.consecutive_losses: int = 0
        self.cooldown_until: float = 0.0  # epoch seconds
        self.hard_stopped: bool = False
        self.halt_reason: str | None = None

        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        try:
            self.current_day = datetime.fromisoformat(data["current_day"]).date() if data.get("current_day") else None
            self.starting_equity = float(data.get("starting_equity", 0.0))
            self.trades_today = int(data.get("trades_today", 0))
            self.consecutive_losses = int(data.get("consecutive_losses", 0))
            self.cooldown_until = float(data.get("cooldown_until", 0.0))
            self.hard_stopped = bool(data.get("hard_stopped", False))
            self.halt_reason = data.get("halt_reason")
        except (KeyError, ValueError, TypeError):
            pass

    def _save_state(self):
        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        data = {
            "current_day": self.current_day.isoformat() if self.current_day else None,
            "starting_equity": self.starting_equity,
            "trades_today": self.trades_today,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until,
            "hard_stopped": self.hard_stopped,
            "halt_reason": self.halt_reason,
        }
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Day reset
    # ------------------------------------------------------------------

    def _reset_if_new_day(self, current_equity: float):
        """Reset daily counters when the UTC date changes.

        On the very first call (current_day is None), we only anchor the day
        and equity WITHOUT resetting trades_today — on_trade_closed() may have
        already counted trades before can_trade() is ever called (e.g. during
        startup recovery). A full reset happens only when the date actually
        changes.
        """
        today = datetime.now(timezone.utc).date()
        if self.current_day == today:
            return
        prev_day = self.current_day
        self.current_day = today
        self.starting_equity = current_equity
        if prev_day is not None:
            # Real day change: full reset
            self.trades_today = 0
            self.cooldown_until = 0.0
            self.hard_stopped = False
            self.halt_reason = None
            logger.info(
                f"TradeThrottle day reset ({prev_day} -> {today}). "
                f"Starting equity: ${current_equity:,.2f}"
            )
        else:
            # First call ever: just anchor day/equity, preserve any trades_today
            logger.info(
                f"TradeThrottle initialized for {today}. "
                f"Starting equity: ${current_equity:,.2f}, trades_today={self.trades_today}"
            )
        self._save_state()

    # ------------------------------------------------------------------
    # Public: can_trade()
    # ------------------------------------------------------------------

    def can_trade(self, current_equity: float = 0.0) -> tuple[bool, str]:
        """Check all throttle gates. Returns (allowed, reason).

        Must be called with the current account equity so the daily loss
        check and day-reset work correctly.
        """
        with self._lock:
            self._reset_if_new_day(current_equity)

            # 1. Hard stop (critical loss streak)
            if self.hard_stopped:
                return False, f"hard_stop_streak: {self.consecutive_losses} consecutive losses >= {self.hard_stop_streak}; trading halted for today"

            # 2. Cooldown active
            now = time.time()
            if self.cooldown_until > 0 and now < self.cooldown_until:
                remaining = int(self.cooldown_until - now)
                return False, f"cooldown_active: {self.consecutive_losses} consecutive losses; wait {remaining}s ({self.cooldown_minutes}min window)"

            # 3. Daily trade limit
            if self.trades_today >= self.max_trades_per_day:
                return False, f"daily_limit_reached: {self.trades_today}/{self.max_trades_per_day} trades today"

            # 4. Daily loss limit (equity-based)
            if self.starting_equity > 0 and current_equity > 0:
                daily_pnl_pct = ((current_equity - self.starting_equity) / self.starting_equity) * 100.0
                if daily_pnl_pct <= -self.max_daily_loss_pct:
                    self.hard_stopped = True
                    self.halt_reason = f"daily_loss_limit: {daily_pnl_pct:.1f}% <= -{self.max_daily_loss_pct}%"
                    self._save_state()
                    return False, self.halt_reason

            return True, "OK"

    # ------------------------------------------------------------------
    # Public: risk_multiplier()
    # ------------------------------------------------------------------

    def risk_multiplier(self) -> float:
        """Risk step-down multiplier based on consecutive loss count.

        Returns 1.0 when no losses, decreasing per risk_step_down_map.
        """
        with self._lock:
            for threshold in sorted(self.risk_step_down_map.keys(), reverse=True):
                if self.consecutive_losses >= threshold:
                    return float(self.risk_step_down_map[threshold])
            return 1.0

    # ------------------------------------------------------------------
    # Public: on_trade_closed()
    # ------------------------------------------------------------------

    def on_trade_closed(self, pnl: float):
        """Update counters after a trade closes.

        Args:
            pnl: realized P&L in USD (positive = profit, negative = loss).
        """
        with self._lock:
            self.trades_today += 1
            # NOTE: do NOT call _reset_if_new_day here — this method receives
            # PnL, not equity. Day reset is handled by can_trade().

            if pnl < 0:
                self.consecutive_losses += 1
                logger.info(
                    f"TradeThrottle: loss #{self.consecutive_losses} "
                    f"(pnl=${pnl:+.2f})"
                )

                # Hard stop
                if self.consecutive_losses >= self.hard_stop_streak:
                    self.hard_stopped = True
                    self.halt_reason = (
                        f"hard_stop_streak: {self.consecutive_losses} consecutive losses "
                        f">= {self.hard_stop_streak}"
                    )
                    logger.warning(f"TradeThrottle: {self.halt_reason}")

                # Cooldown (only if not already hard-stopped)
                elif self.consecutive_losses >= self.loss_streak_threshold:
                    self.cooldown_until = time.time() + (self.cooldown_minutes * 60)
                    logger.warning(
                        f"TradeThrottle: cooldown activated — "
                        f"{self.consecutive_losses} consecutive losses, "
                        f"paused for {self.cooldown_minutes}min"
                    )
            else:
                # Win — reset streak and cooldown
                if self.consecutive_losses > 0:
                    logger.info(
                        f"TradeThrottle: streak reset after {self.consecutive_losses} losses "
                        f"(pnl=${pnl:+.2f})"
                    )
                self.consecutive_losses = 0
                self.cooldown_until = 0.0

            self._save_state()

    # ------------------------------------------------------------------
    # Public: get_state() — for diagnostics / Telegram / logging
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Return a snapshot of the current throttle state for logging/display."""
        with self._lock:
            now = time.time()
            cooldown_remaining = max(0, self.cooldown_until - now) if self.cooldown_until > 0 else 0
            return {
                "current_day": self.current_day.isoformat() if self.current_day else None,
                "trades_today": self.trades_today,
                "max_trades_per_day": self.max_trades_per_day,
                "consecutive_losses": self.consecutive_losses,
                "hard_stopped": self.hard_stopped,
                "cooldown_active": self.cooldown_until > 0 and now < self.cooldown_until,
                "cooldown_remaining_seconds": int(cooldown_remaining),
                "risk_multiplier": self.risk_multiplier(),
                "halt_reason": self.halt_reason,
            }
