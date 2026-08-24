"""HumanizedRiskManager — randomized risk with weighted SL:TP profiles."""

from __future__ import annotations

import random
from typing import Dict, List, Optional, Tuple


class HumanizedRiskManager:
    """Risk 1% ±0.35% jitter, 5% out-of-bounds, 6 weighted SL:TP profiles."""

    RISK_BASE = 0.01
    RISK_JITTER = 0.0035
    OUT_OF_BOUNDS_PROB = 0.05
    OUT_OF_BOUNDS_EXTRA_MIN = 0.001
    OUT_OF_BOUNDS_EXTRA_MAX = 0.005

    LOT_STEP = 0.01
    LOT_JITTER_PROB = 0.15

    # 6 weighted profiles (SL mult : TP mult)
    DEFAULT_PROFILES: List[Dict[str, float]] = [
        {"sl_mult": 1.0, "tp_mult": 1.5, "weight": 0.25},
        {"sl_mult": 1.0, "tp_mult": 1.8, "weight": 0.20},
        {"sl_mult": 1.0, "tp_mult": 2.0, "weight": 0.20},
        {"sl_mult": 1.1, "tp_mult": 1.8, "weight": 0.15},
        {"sl_mult": 1.2, "tp_mult": 2.0, "weight": 0.10},
        {"sl_mult": 1.2, "tp_mult": 2.2, "weight": 0.10},
    ]

    NO_REPEAT_PROB = 0.70

    def __init__(
        self,
        risk_base: float = 0.01,
        seed: Optional[int] = None,
        config: Optional[object] = None,
        profiles: Optional[List[Dict[str, float]]] = None,
    ):
        self._rng = random.Random(seed)
        self.risk_base = risk_base
        self._last_profile_idx: Optional[int] = None

        # Config overrides
        if config is not None:
            self.risk_base = config.risk_base
            self.RISK_BASE = config.risk_base
            self.RISK_JITTER = config.risk_jitter
            self.OUT_OF_BOUNDS_PROB = config.risk_out_of_bounds_prob
            self.OUT_OF_BOUNDS_EXTRA_MIN, self.OUT_OF_BOUNDS_EXTRA_MAX = config.risk_out_of_bounds_extra
            self.LOT_STEP = config.risk_lot_step
            self.LOT_JITTER_PROB = config.risk_lot_jitter_prob
            self.NO_REPEAT_PROB = config.risk_no_repeat_prob
            if config.risk_profiles:
                self._profiles = config.risk_profiles
            else:
                self._profiles = profiles or self.DEFAULT_PROFILES
        else:
            self._profiles = profiles or self.DEFAULT_PROFILES

        # Precompute cumulative weights
        self._rebuild_weights()

    def _rebuild_weights(self):
        total = sum(p["weight"] for p in self._profiles)
        cum = 0.0
        self._cum_weights: List[float] = []
        for p in self._profiles:
            cum += p["weight"] / total
            self._cum_weights.append(cum)

    def _choose_profile_idx(self) -> int:
        """Weighted random choice with 70% no-repeat logic."""
        # First decide candidate via weighted choice
        r = self._rng.random()
        candidate = 0
        for i, cw in enumerate(self._cum_weights):
            if r <= cw:
                candidate = i
                break

        # 70% chance not repeat previous
        if self._last_profile_idx is not None and candidate == self._last_profile_idx:
            if self._rng.random() < self.NO_REPEAT_PROB:
                # Pick different profile, weighted among remaining
                remaining = [(i, p) for i, p in enumerate(self._profiles) if i != self._last_profile_idx]
                if remaining:
                    # Re-normalize weights
                    total_w = sum(p["weight"] for _, p in remaining)
                    r2 = self._rng.random() * total_w
                    acc = 0.0
                    for idx, prof in remaining:
                        acc += prof["weight"]
                        if r2 <= acc:
                            candidate = idx
                            break
        self._last_profile_idx = candidate
        return candidate

    def get_risk_pct(self) -> float:
        """Return risk % with jitter and 5% out-of-bounds chance."""
        jitter = self._rng.uniform(-self.RISK_JITTER, self.RISK_JITTER)
        risk = self.risk_base + jitter

        if self._rng.random() < self.OUT_OF_BOUNDS_PROB:
            # Go out of bounds: add extra beyond jitter range
            extra = self._rng.uniform(self.OUT_OF_BOUNDS_EXTRA_MIN, self.OUT_OF_BOUNDS_EXTRA_MAX)
            # Random direction
            if self._rng.random() < 0.5:
                risk = self.risk_base + self.RISK_JITTER + extra
            else:
                risk = self.risk_base - self.RISK_JITTER - extra
                # Ensure not negative
                risk = max(0.001, risk)

        # Clamp to reasonable [0.1%, 5%] for safety
        risk = max(0.001, min(0.05, risk))
        return round(risk, 5)

    def get_sl_tp_profile(self) -> Dict[str, float]:
        """Return SL:TP multipliers from weighted profiles."""
        idx = self._choose_profile_idx()
        prof = self._profiles[idx]
        # Return copy with slight humanization? Keep as is, but add tiny noise?
        # For realism, add ±0.05 noise to TP mult 10% of time? Spec doesn't say,
        # but we keep profile exact to avoid breaking risk.
        return {
            "sl_mult": float(prof["sl_mult"]),
            "tp_mult": float(prof["tp_mult"]),
            "profile_id": f"profile_{idx}",
            "weight": float(prof["weight"]),
        }

    def get_lot_size(self, base_lot: float) -> float:
        """Apply ±1 step jitter 15% chance to break perfect math."""
        if self._rng.random() < self.LOT_JITTER_PROB:
            # ±1 step
            direction = self._rng.choice([-1, 1])
            new_lot = base_lot + direction * self.LOT_STEP
            # Ensure positive and not below step
            new_lot = max(self.LOT_STEP, new_lot)
            return round(new_lot, 2)
        return round(base_lot, 2)

    def calculate_position_size(
        self,
        equity: float,
        risk_pct: float,
        entry: float,
        stop: float,
        point_value_lot: float = 100.0,
    ) -> float:
        """Calculate lot size from equity, risk %, entry/stop distance."""
        if entry == stop:
            return self.LOT_STEP
        risk_cash = equity * risk_pct
        price_dist = abs(entry - stop)
        # Simplified: lot = risk_cash / (price_dist * point_value_lot)
        # Avoid division by zero
        if price_dist <= 0:
            return self.LOT_STEP
        raw_lot = risk_cash / (price_dist * point_value_lot)
        # Quantize to lot step
        steps = round(raw_lot / self.LOT_STEP)
        lot = max(self.LOT_STEP, steps * self.LOT_STEP)
        # Apply jitter
        return self.get_lot_size(lot)

    def reset(self):
        self._last_profile_idx = None
