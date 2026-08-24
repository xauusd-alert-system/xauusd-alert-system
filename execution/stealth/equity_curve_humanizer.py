"""EquityCurveHumanizer — partial exits, early closes, manual trailing for both MT5 and UTEx."""

from __future__ import annotations

import random
from typing import Dict, List, Optional


class EquityCurveHumanizer:
    """Human-like position management.

    MT5:
    - 25% chance partial exit 30-50% at +1R
    - 12% chance early close at 0.6×TP
    - Manual trailing 15-40 pips at +1.5R (XAUUSD)

    UTEx challenge (stocks):
    - Same probs but shares rounding to whole shares
    - Trailing $0.50-$2.00 depending on asset price at +1.5R
    """

    PARTIAL_EXIT_PROB = 0.25
    PARTIAL_EXIT_MIN_PCT = 0.30
    PARTIAL_EXIT_MAX_PCT = 0.50
    PARTIAL_EXIT_TRIGGER_R = 1.0

    EARLY_CLOSE_PROB = 0.12
    EARLY_CLOSE_TRIGGER_TP_RATIO = 0.6

    TRAILING_START_R = 1.5
    TRAILING_MIN_PIPS = 15
    TRAILING_MAX_PIPS = 40
    PIP_VALUE = 0.1  # XAUUSD

    TRAILING_MIN_DOLLARS = 0.50
    TRAILING_MAX_DOLLARS = 2.00

    def __init__(
        self,
        seed: Optional[int] = None,
        pip_value: float = 0.1,
        config: Optional[object] = None,
    ):
        self._rng = random.Random(seed)
        self.pip_value = pip_value

        if config is not None:
            self.PARTIAL_EXIT_PROB = config.partial_exit_prob
            self.PARTIAL_EXIT_MIN_PCT, self.PARTIAL_EXIT_MAX_PCT = config.partial_exit_pct_range
            self.PARTIAL_EXIT_TRIGGER_R = config.partial_exit_trigger_r
            self.EARLY_CLOSE_PROB = config.early_close_prob
            self.EARLY_CLOSE_TRIGGER_TP_RATIO = config.early_close_trigger_tp_ratio
            self.TRAILING_START_R = config.trailing_start_r
            self.TRAILING_MIN_PIPS, self.TRAILING_MAX_PIPS = config.trailing_pips_range
            self.TRAILING_MIN_DOLLARS, self.TRAILING_MAX_DOLLARS = config.trailing_dollars_range
            self.pip_value = config.pip_value
            self.PIP_VALUE = config.pip_value

        self._partial_done: set = set()
        self._trailing_active: set = set()

    def _calc_r(self, position: Dict) -> float:
        try:
            entry = float(position.get("entry_price", 0))
            current = float(position.get("current_price", 0))
            stop = float(position.get("stop_price", 0))
            side = position.get("side", "long")
            if entry == stop:
                return 0.0
            if side == "long":
                risk_dist = entry - stop
                if risk_dist <= 0:
                    return 0.0
                return (current - entry) / risk_dist
            else:
                risk_dist = stop - entry
                if risk_dist <= 0:
                    return 0.0
                return (entry - current) / risk_dist
        except Exception:
            return 0.0

    def should_partial_exit(self, position: Dict) -> bool:
        pos_id = position.get("id") or position.get("ticket") or id(position)
        if pos_id in self._partial_done:
            return False
        r = self._calc_r(position)
        if r < self.PARTIAL_EXIT_TRIGGER_R:
            return False
        if self._rng.random() < self.PARTIAL_EXIT_PROB:
            self._partial_done.add(pos_id)
            return True
        return False

    def get_partial_exit_pct(self) -> float:
        return round(self._rng.uniform(self.PARTIAL_EXIT_MIN_PCT, self.PARTIAL_EXIT_MAX_PCT), 3)

    def get_partial_exit_shares(self, total_shares: int) -> int:
        """For stocks: 30-50% rounded to whole shares, at least 1."""
        pct = self.get_partial_exit_pct()
        shares = max(1, int(total_shares * pct))
        # Ensure not all shares (leave at least 1)
        if shares >= total_shares:
            shares = max(1, total_shares - 1)
        return shares

    def should_early_close(self, position: Dict) -> bool:
        try:
            entry = float(position.get("entry_price", 0))
            current = float(position.get("current_price", 0))
            tp = float(position.get("tp_price", 0))
            side = position.get("side", "long")
            if entry == 0 or tp == 0:
                return False
            if side == "long":
                total_dist = tp - entry
                if total_dist <= 0:
                    return False
                prog = (current - entry) / total_dist
            else:
                total_dist = entry - tp
                if total_dist <= 0:
                    return False
                prog = (entry - current) / total_dist
            if prog < self.EARLY_CLOSE_TRIGGER_TP_RATIO:
                return False
            return self._rng.random() < self.EARLY_CLOSE_PROB
        except Exception:
            return False

    def should_trail(self, position: Dict) -> bool:
        r = self._calc_r(position)
        return r >= self.TRAILING_START_R

    def get_trailing_distance_price(self) -> float:
        pips = self._rng.randint(self.TRAILING_MIN_PIPS, self.TRAILING_MAX_PIPS)
        return round(pips * self.PIP_VALUE, 4)

    def get_trailing_distance_pips(self) -> int:
        return self._rng.randint(self.TRAILING_MIN_PIPS, self.TRAILING_MAX_PIPS)

    def get_trailing_distance_dollars(self, asset_price: Optional[float] = None) -> float:
        """For UTEx challenge: $0.50-$2.00 depending on asset price."""
        if asset_price is None:
            return round(self._rng.uniform(self.TRAILING_MIN_DOLLARS, self.TRAILING_MAX_DOLLARS), 2)

        # Scale with price: cheap stocks (<50) => 0.5-1.0, mid 50-200 => 0.75-1.5, expensive >200 => 1.0-2.0
        if asset_price < 50:
            low, high = 0.50, 1.00
        elif asset_price < 200:
            low, high = 0.75, 1.50
        else:
            low, high = 1.00, 2.00

        # Allow config override to still clamp to overall min/max
        low = max(low, self.TRAILING_MIN_DOLLARS)
        high = min(high, self.TRAILING_MAX_DOLLARS)
        return round(self._rng.uniform(low, high), 2)

    def manage_position(self, position: Dict) -> List[Dict]:
        actions: List[Dict] = []

        if self.should_partial_exit(position):
            total_shares = position.get("qty") or position.get("volume") or position.get("shares")
            if total_shares is not None and isinstance(total_shares, (int, float)) and total_shares > 1:
                # For shares, return share count
                try:
                    shares = self.get_partial_exit_shares(int(total_shares))
                    actions.append({
                        "type": "partial_exit",
                        "pct": round(shares / int(total_shares), 3),
                        "shares": shares,
                        "trigger_r": self.PARTIAL_EXIT_TRIGGER_R,
                    })
                except Exception:
                    actions.append({
                        "type": "partial_exit",
                        "pct": self.get_partial_exit_pct(),
                        "trigger_r": self.PARTIAL_EXIT_TRIGGER_R,
                    })
            else:
                actions.append({
                    "type": "partial_exit",
                    "pct": self.get_partial_exit_pct(),
                    "trigger_r": self.PARTIAL_EXIT_TRIGGER_R,
                })

        if self.should_early_close(position):
            actions.append({
                "type": "early_close",
                "trigger_tp_ratio": self.EARLY_CLOSE_TRIGGER_TP_RATIO,
            })

        if self.should_trail(position):
            pos_id = position.get("id") or position.get("ticket") or id(position)
            self._trailing_active.add(pos_id)
            asset_price = position.get("current_price") or position.get("entry_price")
            actions.append({
                "type": "trailing",
                "distance_price": self.get_trailing_distance_price(),
                "distance_pips": self.get_trailing_distance_pips(),
                "distance_dollars": self.get_trailing_distance_dollars(asset_price),
                "trigger_r": self.TRAILING_START_R,
            })

        return actions

    def reset(self):
        self._partial_done.clear()
        self._trailing_active.clear()
