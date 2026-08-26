"""HumanizedRiskManager — challenge-specific risk sizing with jitter.

Tracks realtime floating equity and enforces daily ($30) and overall ($90)
hard stops with humanized jitter on position sizing.  All constants live
inside the class; no external hard-coding.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("stealth.risk")

# ---------------------------------------------------------------------------
# SL:TP profile pool (6 weighted profiles)
# ---------------------------------------------------------------------------
_SL_TP_PROFILES: List[Tuple[float, float, float]] = [
    # (risk_reward_ratio, sl_mult, tp_mult)  — weight at index i
    (1.0 / 1.5, 1.0, 1.5),   # 1:1.5  — w 0.25
    (1.0 / 1.7, 1.0, 1.7),   # 1:1.7  — w 0.20
    (1.0 / 1.8, 1.0, 1.8),   # 1:1.8  — w 0.20
    (1.0 / 2.0, 1.0, 2.0),   # 1:2.0  — w 0.15
    (1.2 / 2.0, 1.2, 2.0),   # 1.2:2.0 — w 0.10
    (1.2 / 2.2, 1.2, 2.2),   # 1.2:2.2 — w 0.10
]
_PROFILE_WEIGHTS = [0.25, 0.20, 0.20, 0.15, 0.10, 0.10]


class HumanizedRiskManager:
    """Realtime floating-equity risk manager for the Hash Hedge challenge.

    Parameters
    ----------
    start_balance : float
        Starting account balance (challenge = $1 000).
    risk_base_pct : float
        Base risk fraction per trade (default 1 %).
    seed : int | None
        Optional RNG seed.
    cfg : dict | None
        Optional override dict (daily_hard_stop, overall_buffer, etc.).
    """

    # --- Class-level defaults (all tunables inside) ---
    DAILY_HARD_STOP: float = -30.0    # shut down at -$30 floating daily
    OVERALL_BUFFER: float = -90.0     # shut down at -$90 floating overall
    PLATFORM_DAILY_LIMIT: float = -50.0
    PLATFORM_OVERALL_LIMIT: float = -100.0
    LEVERAGE: float = 5.0
    START_BALANCE: float = 1000.0
    PROFIT_TARGET: float = 80.0       # 8 %

    JITTER_RANGE: float = 0.35        # ±0.35 % around base risk
    OOB_CHANCE: float = 0.05          # 5 % chance out-of-bounds sizing
    PROFILE_CHANCE_NO_REPEAT: float = 0.70
    SIZE_VARIATION_CHANCE: float = 0.15  # ±1 share

    def __init__(
        self,
        *,
        start_balance: float = START_BALANCE,
        risk_base_pct: float = 0.01,
        seed: int | None = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        c = cfg or {}
        self._rng = random.Random(seed)
        self.start_balance = c.get("start_balance", start_balance)
        self.risk_base_pct = c.get("risk_base_pct", risk_base_pct)
        self.daily_hard_stop = c.get("daily_hard_stop", self.DAILY_HARD_STOP)
        self.overall_buffer = c.get("overall_buffer", self.OVERALL_BUFFER)
        self.leverage = c.get("leverage", self.LEVERAGE)
        self.profit_target = c.get("profit_target", self.PROFIT_TARGET)

        # Realtime tracking
        self._balance_at_day_start: float = self.start_balance
        self._last_reset_ts: Optional[datetime] = None
        self._daily_closed_pnl: float = 0.0
        self._floating_pnl: float = 0.0
        self._equity: float = self.start_balance

    # ------------------------------------------------------------------
    # Daily reset (00:00-00:13 UTC+4 → 20:00-20:13 UTC)
    # ------------------------------------------------------------------

    @staticmethod
    def _utc4_to_utc(dt_utc4: datetime) -> datetime:
        """Convert a naive UTC+4 datetime to naive UTC."""
        return dt_utc4 - timedelta(hours=4)

    @staticmethod
    def _is_in_reset_window_utc(utc_now: datetime) -> bool:
        """True when UTC time is in 20:00-20:13 (== 00:00-00:13 UTC+4)."""
        h, m = utc_now.hour, utc_now.minute
        return (h == 20 and m <= 13) or (h == 0 and m <= 13)  # wrap-aware

    def _is_in_reset_window(self, now_utc4: Optional[datetime] = None) -> bool:
        """Check whether *now* (naive UTC+4) falls in the reset window."""
        if now_utc4 is None:
            return False
        utc = self._utc4_to_utc(now_utc4)
        return self._is_in_reset_window_utc(utc)

    def _maybe_reset_day(self, now_utc4: datetime) -> None:
        """Reset daily counters if we crossed into the reset window."""
        if not self._is_in_reset_window(now_utc4):
            return
        if self._last_reset_ts is not None:
            # Only reset once per window
            last_utc = self._utc4_to_utc(self._last_reset_ts)
            cur_utc = self._utc4_to_utc(now_utc4)
            if (cur_utc - last_utc).total_seconds() < 1800:
                return  # already reset within this window
        prev_daily = self._daily_closed_pnl
        self._balance_at_day_start = self._equity
        self._daily_closed_pnl = 0.0
        self._last_reset_ts = now_utc4
        logger.info(
            "daily reset: balance_at_day_start=%.2f (prev closed_pnl=%.2f)",
            self._balance_at_day_start, prev_daily,
        )

    # ------------------------------------------------------------------
    # Realtime equity feed
    # ------------------------------------------------------------------

    def update_floating_pnl(
        self,
        floating_pnl: float,
        equity: float,
        now_utc4: datetime,
    ) -> None:
        """Feed realtime floating PnL from the platform snapshot.

        Called every poll cycle (2-5 s).
        """
        # Update equity BEFORE reset so the reset uses the current value
        self._floating_pnl = floating_pnl
        self._equity = equity
        self._maybe_reset_day(now_utc4)

    def update_closed_pnl(self, closed_pnl: float) -> None:
        """Accumulate closed PnL into the daily bucket."""
        self._daily_closed_pnl += closed_pnl

    # ------------------------------------------------------------------
    # PnL queries
    # ------------------------------------------------------------------

    def daily_pnl(self) -> float:
        """Floating + closed since reset."""
        return self._floating_pnl + self._daily_closed_pnl

    def overall_pnl(self) -> float:
        """Equity − start_balance."""
        return self._equity - self.start_balance

    # ------------------------------------------------------------------
    # Limit checks
    # ------------------------------------------------------------------

    def is_daily_loss_limit_hit(self) -> bool:
        """True when *daily* PnL has breached the hard stop (-$30)."""
        return self.daily_pnl() <= self.daily_hard_stop

    def is_overall_loss_buffer_hit(self) -> bool:
        """True when *overall* PnL has breached the buffer (-$90)."""
        return self.overall_pnl() <= self.overall_buffer

    def is_platform_daily_breach(self) -> bool:
        """True when daily PnL would breach the *platform's* $50 limit."""
        return self.daily_pnl() <= self.PLATFORM_DAILY_LIMIT

    def is_platform_overall_breach(self) -> bool:
        """True when overall PnL would breach the *platform's* $100 limit."""
        return self.overall_pnl() <= self.PLATFORM_OVERALL_LIMIT

    # ------------------------------------------------------------------
    # Gate: can_trade
    # ------------------------------------------------------------------

    def can_trade(
        self,
        daily_pnl: Optional[float] = None,
        overall_pnl: Optional[float] = None,
        now_utc4: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Return (allowed, reason).

        Accepts optional overrides for the caller that already has a
        snapshot; falls back to internal trackers.
        """
        if now_utc4 is not None:
            self._maybe_reset_day(now_utc4)

        dp = daily_pnl if daily_pnl is not None else self.daily_pnl()
        op = overall_pnl if overall_pnl is not None else self.overall_pnl()

        if op <= self.overall_buffer:
            return False, f"overall loss {op:.2f} <= {self.overall_buffer}"
        if dp <= self.daily_hard_stop:
            return False, f"daily loss {dp:.2f} <= {self.daily_hard_stop}"
        if self._equity <= 0:
            return False, "equity is zero or negative"
        return True, "ok"

    # ------------------------------------------------------------------
    # Force-close signal (continuous monitor)
    # ------------------------------------------------------------------

    def should_force_close(self) -> bool:
        """True when any position should be force-closed immediately."""
        return self.is_daily_loss_limit_hit() or self.is_overall_loss_buffer_hit()

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def _jittered_risk_pct(self) -> float:
        """Apply jitter to the base risk %.

        5 % chance of being *out-of-bounds* (slightly wider).
        """
        base = self.risk_base_pct
        if self._rng.random() < self.OOB_CHANCE:
            # Out-of-bounds: go 1.5-2x the base
            return base * self._rng.uniform(1.5, 2.0)
        return base * self._rng.uniform(
            1.0 - self.JITTER_RANGE, 1.0 + self.JITTER_RANGE,
        )

    def risk_usd(self) -> float:
        """Humanized risk in USD for the current equity."""
        pct = self._jittered_risk_pct()
        return self._equity * pct

    def position_size(self, stop_distance: float, price: float) -> int:
        """Compute shares to buy, respecting buying power and leverage.

        ``stop_distance`` is the dollar distance from entry to SL.
        Returns 0 if sizing < 1 share.
        """
        if stop_distance <= 0 or price <= 0 or self._equity <= 0:
            return 0

        risk = self.risk_usd()
        shares = risk / stop_distance

        # ±1 share jitter (15 % chance)
        if self._rng.random() < self.SIZE_VARIATION_CHANCE:
            shares += self._rng.choice([-1, 1])

        # Buying power cap: equity * leverage / price
        max_shares = int((self._equity * self.leverage) / price)
        shares = min(shares, max_shares)
        return max(0, int(shares))

    def notional_ok(self, shares: int, price: float) -> bool:
        """Verify notional ≤ buying power."""
        if price <= 0 or shares <= 0:
            return False
        notional = shares * price
        buying_power = self._equity * self.leverage
        return notional <= buying_power

    # ------------------------------------------------------------------
    # SL:TP profile selection
    # ------------------------------------------------------------------

    _prev_profile_idx: Optional[int] = None

    def select_sl_tp_profile(self) -> Tuple[float, float]:
        """Return (sl_distance_multiplier, tp_distance_multiplier).

        Picks a weighted random profile, preferring not to repeat the
        previous one (70 % chance of switching).
        """
        profiles = _SL_TP_PROFILES
        weights = list(_PROFILE_WEIGHTS)

        if (
            self._prev_profile_idx is not None
            and self._rng.random() < self.PROFILE_CHANCE_NO_REPEAT
        ):
            weights[self._prev_profile_idx] = 0.0
            total = sum(weights)
            if total > 0:
                weights = [w / total for w in weights]
            else:
                weights = list(_PROFILE_WEIGHTS)

        idx = self._rng.choices(range(len(profiles)), weights=weights, k=1)[0]
        self._prev_profile_idx = idx
        _, sl_m, tp_m = profiles[idx]
        return sl_m, tp_m

    # ------------------------------------------------------------------
    # Equity accessors (for integration)
    # ------------------------------------------------------------------

    @property
    def equity(self) -> float:
        return self._equity

    @equity.setter
    def equity(self, value: float) -> None:
        self._equity = value

    @property
    def balance_at_day_start(self) -> float:
        return self._balance_at_day_start

    def reset_counters(self) -> None:
        """Manual reset (e.g. new trading day outside the window)."""
        self._daily_closed_pnl = 0.0
        self._floating_pnl = 0.0
