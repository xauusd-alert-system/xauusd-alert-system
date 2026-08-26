"""EquityCurveHumanizer — makes the equity curve look manually traded.

Handles partial exits, early closes, and manual-style trailing stops.
UTEx has no native trailing — we simulate it by modifying the SL
through the browser with humanized delays.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("stealth.equity")


class EquityCurveHumanizer:
    """Humanizes position exits to avoid detection of bot patterns.

    Parameters
    ----------
    seed : int | None
        Optional RNG seed.
    cfg : dict | None
        Override dict for probabilities and dollar distances.
    """

    # --- Defaults ---
    PARTIAL_EXIT_CHANCE: float = 0.25    # 25 % at +1R
    PARTIAL_EXIT_MIN: float = 0.30       # 30 % of position
    PARTIAL_EXIT_MAX: float = 0.50       # 50 % of position
    EARLY_CLOSE_CHANCE: float = 0.12     # 12 % at 0.6×TP

    # Trailing distance by price tier (dollars)
    # price < 50 → $0.50-1.00,  50-200 → $0.75-1.50,  >200 → $1.00-2.00
    TRAIL_TIER_1_MAX: float = 50.0
    TRAIL_TIER_2_MAX: float = 200.0
    TRAIL_TIER_1: Tuple[float, float] = (0.50, 1.00)
    TRAIL_TIER_2: Tuple[float, float] = (0.75, 1.50)
    TRAIL_TIER_3: Tuple[float, float] = (1.00, 2.00)

    # Trailing activation R-level
    TRAIL_ACTIVATION_R: float = 1.5

    def __init__(
        self,
        *,
        seed: int | None = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        c = cfg or {}
        self._rng = random.Random(seed)
        self.partial_exit_chance = c.get("partial_exit_chance", self.PARTIAL_EXIT_CHANCE)
        self.partial_exit_min = c.get("partial_exit_min", self.PARTIAL_EXIT_MIN)
        self.partial_exit_max = c.get("partial_exit_max", self.PARTIAL_EXIT_MAX)
        self.early_close_chance = c.get("early_close_chance", self.EARLY_CLOSE_CHANCE)
        self.trail_activation_r = c.get("trail_activation_r", self.TRAIL_ACTIVATION_R)

    # ------------------------------------------------------------------
    # Partial exit at +1R
    # ------------------------------------------------------------------

    def should_partial_exit(self, r_multiple: float) -> bool:
        """True if we should do a partial exit at the given R-level.

        Only triggers at +1R or beyond.
        """
        if r_multiple < 1.0:
            return False
        return self._rng.random() < self.partial_exit_chance

    def get_partial_exit_shares(self, total_shares: int) -> int:
        """How many shares to close in a partial exit.

        Returns 30-50 % of total, rounded to integer shares.
        """
        fraction = self._rng.uniform(self.partial_exit_min, self.partial_exit_max)
        shares = int(round(total_shares * fraction))
        return max(1, min(shares, total_shares - 1))

    # ------------------------------------------------------------------
    # Early close at 0.6×TP
    # ------------------------------------------------------------------

    def should_early_close(self, r_multiple: float) -> bool:
        """True if we close before TP at ~0.6× the TP distance."""
        if r_multiple < 0.6:
            return False
        if r_multiple >= 1.0:
            return False
        return self._rng.random() < self.early_close_chance

    # ------------------------------------------------------------------
    # Manual trailing stop
    # ------------------------------------------------------------------

    def get_trailing_distance_dollars(self, price: float) -> float:
        """Trailing distance in dollars based on price tier + ATR factor.

        Uses price tiers from the spec:
            price < $50   → $0.50-1.00
            $50-200       → $0.75-1.50
            > $200        → $1.00-2.00
        """
        if price <= self.TRAIL_TIER_1_MAX:
            lo, hi = self.TRAIL_TIER_1
        elif price <= self.TRAIL_TIER_2_MAX:
            lo, hi = self.TRAIL_TIER_2
        else:
            lo, hi = self.TRAIL_TIER_3
        return self._rng.uniform(lo, hi)

    def compute_trailing_sl(
        self,
        side: str,
        current_price: float,
        entry_price: float,
        current_sl: float,
    ) -> Optional[float]:
        """Compute a new trailing SL if it should be moved.

        Returns the new SL price if it should be tightened,
        or None if no change.

        Trailing only activates at +1.5R.
        """
        risk_dist = abs(entry_price - current_sl)
        if risk_dist <= 0:
            return None

        if side == "long":
            unrealized = current_price - entry_price
        else:
            unrealized = entry_price - current_price

        r_mult = unrealized / risk_dist if risk_dist > 0 else 0.0

        if r_mult < self.trail_activation_r:
            return None

        trail_dist = self.get_trailing_distance_dollars(current_price)

        if side == "long":
            new_sl = current_price - trail_dist
            if new_sl > current_sl:
                return round(new_sl, 2)
        else:
            new_sl = current_price + trail_dist
            if new_sl < current_sl:
                return round(new_sl, 2)

        return None

    # ------------------------------------------------------------------
    # Position management decisions
    # ------------------------------------------------------------------

    def evaluate_position(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        current_sl: float,
        tp_price: float,
        total_shares: int,
        remaining_shares: int,
        already_partialed: bool,
    ) -> List[Dict[str, Any]]:
        """Evaluate a position and return a list of actions.

        Actions returned:
            {"action": "partial_exit", "shares": N}
            {"action": "early_close"}
            {"action": "trailing_sl", "new_sl": float}
        """
        actions: List[Dict[str, Any]] = []

        risk_dist = abs(entry_price - current_sl)
        if risk_dist <= 0:
            return actions

        if side == "long":
            unrealized = current_price - entry_price
        else:
            unrealized = entry_price - current_price

        r_mult = unrealized / risk_dist if risk_dist > 0 else 0.0

        # Partial exit at +1R
        if not already_partialed and self.should_partial_exit(r_mult):
            shares = self.get_partial_exit_shares(remaining_shares)
            actions.append({"action": "partial_exit", "shares": shares})

        # Early close at 0.6×TP
        if self.should_early_close(r_mult):
            actions.append({"action": "early_close"})

        # Trailing SL at +1.5R
        new_sl = self.compute_trailing_sl(side, current_price, entry_price, current_sl)
        if new_sl is not None:
            actions.append({"action": "trailing_sl", "new_sl": new_sl})

        return actions
