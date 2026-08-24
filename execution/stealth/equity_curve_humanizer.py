"""EquityCurveHumanizer — partial exits, early closes, manual trailing."""

from __future__ import annotations

import random
from typing import Dict, List, Optional, Tuple


class EquityCurveHumanizer:
    """Human-like position management.

    - 25% chance partial exit 30-50% at +1R
    - 12% chance early close at 0.6×TP
    - Manual trailing 15-40 pips at +1.5R (XAUUSD)
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
            self.pip_value = config.pip_value
            self.PIP_VALUE = config.pip_value

        # Track which positions already had partial exit to avoid repeat
        self._partial_done: set = set()
        self._trailing_active: set = set()

    def _calc_r(self, position: Dict) -> float:
        """Calculate current R multiple from position dict.

        Expected position keys:
          entry_price, current_price, stop_price, side ('long'/'short')
        R = (current - entry) / (entry - stop) for long, etc.
        Returns 0 if invalid.
        """
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
        """25% chance partial exit at +1R, once per position."""
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
        """30-50% partial exit."""
        return round(self._rng.uniform(self.PARTIAL_EXIT_MIN_PCT, self.PARTIAL_EXIT_MAX_PCT), 3)

    def should_early_close(self, position: Dict) -> bool:
        """12% chance early close at 0.6×TP.

        We approximate TP progress via R: if TP is at e.g. 2R, then 0.6×TP ~1.2R.
        For simplicity, use trigger based on current price vs TP.
        """
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
        """Check if trailing should activate at +1.5R."""
        r = self._calc_r(position)
        return r >= self.TRAILING_START_R

    def get_trailing_distance_price(self) -> float:
        """15-40 pips trailing distance in price units."""
        pips = self._rng.randint(self.TRAILING_MIN_PIPS, self.TRAILING_MAX_PIPS)
        return round(pips * self.PIP_VALUE, 4)

    def get_trailing_distance_pips(self) -> int:
        return self._rng.randint(self.TRAILING_MIN_PIPS, self.TRAILING_MAX_PIPS)

    def manage_position(self, position: Dict) -> List[Dict]:
        """Main entry: return list of action dicts for given position.

        Position dict expected:
          {
            'id' or 'ticket': unique,
            'entry_price': float,
            'current_price': float,
            'stop_price': float,
            'tp_price': float,
            'side': 'long'|'short',
            'volume': float,
          }

        Returns list of actions:
          {'type': 'partial_exit', 'pct': 0.35, 'delay_sec': ...}
          {'type': 'early_close', 'delay_sec': ...}
          {'type': 'trailing', 'distance_price': ..., 'distance_pips': ..., 'delay_sec': ...}
        """
        actions: List[Dict] = []

        if self.should_partial_exit(position):
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
            # Only activate trailing once per position, but keep updating distance?
            # For simplicity, allow repeated trailing actions but mark active.
            self._trailing_active.add(pos_id)
            actions.append({
                "type": "trailing",
                "distance_price": self.get_trailing_distance_price(),
                "distance_pips": self.get_trailing_distance_pips(),
                "trigger_r": self.TRAILING_START_R,
            })

        return actions

    def reset(self):
        self._partial_done.clear()
        self._trailing_active.clear()
