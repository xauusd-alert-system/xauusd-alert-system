"""HumanizedRiskManager — randomized risk with realtime floating equity tracking."""

from __future__ import annotations

import random
from datetime import datetime, date, time as dt_time, timezone, timedelta
from typing import Dict, List, Optional, Tuple


def _parse_hm(hm: str) -> dt_time:
    h, m = map(int, hm.split(":"))
    return dt_time(h, m)


class HumanizedRiskManager:
    """Risk 1% ±0.35% jitter, 5% out-of-bounds, 6 weighted SL:TP profiles, shares, floating PnL limits."""

    RISK_BASE = 0.01
    RISK_JITTER = 0.0035
    OUT_OF_BOUNDS_PROB = 0.05
    OUT_OF_BOUNDS_EXTRA_MIN = 0.001
    OUT_OF_BOUNDS_EXTRA_MAX = 0.005

    RISK_JITTER_RANGE_MIN = 0.007
    RISK_JITTER_RANGE_MAX = 0.013  # 0.7-1.3% for challenge

    LOT_STEP = 0.01
    LOT_JITTER_PROB = 0.15

    DEFAULT_PROFILES: List[Dict[str, float]] = [
        {"sl_mult": 1.0, "tp_mult": 1.5, "weight": 0.25},
        {"sl_mult": 1.0, "tp_mult": 1.8, "weight": 0.20},
        {"sl_mult": 1.0, "tp_mult": 2.0, "weight": 0.20},
        {"sl_mult": 1.1, "tp_mult": 1.8, "weight": 0.15},
        {"sl_mult": 1.2, "tp_mult": 2.0, "weight": 0.10},
        {"sl_mult": 1.2, "tp_mult": 2.2, "weight": 0.10},
    ]

    NO_REPEAT_PROB = 0.70

    # Challenge limits (confirmed Hash Hedge rules)
    DAILY_LOSS_LIMIT = 50.0  # platform limit
    MAX_OVERALL_LOSS = 100.0
    DAILY_HARD_STOP = 30.0  # bot hard stop floating, before platform -$50
    OVERALL_BUFFER = 10.0  # buffer $10 -> stop at -$90 floating
    STARTING_BALANCE = 1000.0

    # Daily reset window UTC+4: 00:00-00:13 UTC+4 = 20:00-20:13 UTC
    DAILY_RESET_START_UTC4 = "00:00"
    DAILY_RESET_END_UTC4 = "00:13"
    DAILY_RESET_OFFSET_HOURS = 4

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

        # Floating equity tracking (realtime)
        self._current_day: Optional[date] = None
        self._daily_pnl: float = 0.0  # closed + floating since reset
        self._overall_pnl: float = 0.0  # total floating + closed vs start
        self._floating_pnl: float = 0.0
        self._closed_pnl_since_reset: float = 0.0
        self._balance_at_day_start: float = self.STARTING_BALANCE
        self._balance_at_start: float = self.STARTING_BALANCE
        self._daily_hard_stopped: bool = False
        self._overall_hard_stopped: bool = False
        self._last_reset_date: Optional[date] = None

        # Reset window
        self._reset_start_min = 0
        self._reset_end_min = 13
        self._reset_offset = self.DAILY_RESET_OFFSET_HOURS

        if config is not None:
            self.risk_base = config.risk_base
            self.RISK_BASE = config.risk_base
            self.RISK_JITTER = config.risk_jitter
            self.OUT_OF_BOUNDS_PROB = config.risk_out_of_bounds_prob
            self.OUT_OF_BOUNDS_EXTRA_MIN, self.OUT_OF_BOUNDS_EXTRA_MAX = config.risk_out_of_bounds_extra
            self.LOT_STEP = config.risk_lot_step
            self.LOT_JITTER_PROB = config.risk_lot_jitter_prob
            self.NO_REPEAT_PROB = config.risk_no_repeat_prob
            self.RISK_JITTER_RANGE_MIN, self.RISK_JITTER_RANGE_MAX = config.risk_jitter_range
            self.DAILY_LOSS_LIMIT = config.challenge_daily_loss_limit
            self.MAX_OVERALL_LOSS = config.challenge_max_overall_loss
            self.DAILY_HARD_STOP = config.challenge_daily_hard_stop
            self.OVERALL_BUFFER = config.challenge_overall_buffer
            self.STARTING_BALANCE = config.challenge_starting_balance
            self._balance_at_day_start = self.STARTING_BALANCE
            self._balance_at_start = self.STARTING_BALANCE

            # Reset window
            if hasattr(config, "challenge_daily_reset_window_utc4"):
                start_str, end_str = config.challenge_daily_reset_window_utc4
                self._reset_start_min = _parse_hm(start_str).hour * 60 + _parse_hm(start_str).minute
                self._reset_end_min = _parse_hm(end_str).hour * 60 + _parse_hm(end_str).minute
            if hasattr(config, "challenge_daily_reset_offset_hours"):
                self._reset_offset = config.challenge_daily_reset_offset_hours

            if config.risk_profiles:
                self._profiles = config.risk_profiles
            else:
                self._profiles = profiles or self.DEFAULT_PROFILES
        else:
            self._profiles = profiles or self.DEFAULT_PROFILES

        self._rebuild_weights()

    def _rebuild_weights(self):
        total = sum(p["weight"] for p in self._profiles)
        cum = 0.0
        self._cum_weights: List[float] = []
        for p in self._profiles:
            cum += p["weight"] / total
            self._cum_weights.append(cum)

    def _choose_profile_idx(self) -> int:
        r = self._rng.random()
        candidate = 0
        for i, cw in enumerate(self._cum_weights):
            if r <= cw:
                candidate = i
                break
        if self._last_profile_idx is not None and candidate == self._last_profile_idx:
            if self._rng.random() < self.NO_REPEAT_PROB:
                remaining = [(i, p) for i, p in enumerate(self._profiles) if i != self._last_profile_idx]
                if remaining:
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

    def _is_in_reset_window(self, now_utc: datetime) -> bool:
        """Check if now_utc is in daily reset window 00:00-00:13 UTC+4."""
        # Convert UTC to UTC+4
        utc4 = now_utc + timedelta(hours=self._reset_offset)
        minutes = utc4.hour * 60 + utc4.minute
        return self._reset_start_min <= minutes < self._reset_end_min

    def _ensure_day(self, now: datetime):
        """Reset daily state on new day or in reset window."""
        # Handle reset window logic: if in reset window and not yet reset today (UTC+4 date)
        if isinstance(now, datetime):
            now_utc = now
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            utc4 = now_utc + timedelta(hours=self._reset_offset)
            utc4_date = utc4.date()

            # If we are in reset window and haven't reset for this UTC+4 date, do reset
            if self._is_in_reset_window(now_utc):
                if self._last_reset_date != utc4_date:
                    # Daily reset: balance_at_day_start = current equity (balance + floating)
                    # For simplicity, we reset daily PnL counters
                    self._balance_at_day_start = self._balance_at_start + self._overall_pnl
                    self._closed_pnl_since_reset = 0.0
                    self._daily_pnl = self._floating_pnl  # floating carries over? Actually daily loss should be floating + closed since reset
                    self._daily_hard_stopped = False
                    self._last_reset_date = utc4_date
                    self._current_day = now.date()
                    return

            # Normal day change detection (fallback)
            d = now.date()
            if self._current_day != d:
                # Only reset if not already reset via reset window logic today
                if self._last_reset_date != utc4_date:
                    self._current_day = d
                    self._daily_pnl = self._floating_pnl
                    self._closed_pnl_since_reset = 0.0
                    self._daily_hard_stopped = False

    def update_floating_pnl(self, floating_pnl: float, equity: Optional[float] = None, now: Optional[datetime] = None):
        """Update realtime floating PnL and check hard stops.

        Formula: daily_loss = (balance_at_day_start + floating_pnl + closed_since_reset) - balance_at_day_start
                = floating_pnl + closed_since_reset
        """
        if now is not None:
            self._ensure_day(now)

        self._floating_pnl = floating_pnl
        # Daily loss is floating + closed since reset
        self._daily_pnl = self._floating_pnl + self._closed_pnl_since_reset
        # Overall is vs starting balance
        if equity is not None:
            self._overall_pnl = equity - self._balance_at_start
        else:
            # Approximate: overall = daily + previous days? We keep incremental
            pass

        if self._daily_pnl <= -self.DAILY_HARD_STOP:
            self._daily_hard_stopped = True
        if self._overall_pnl <= -(self.MAX_OVERALL_LOSS - self.OVERALL_BUFFER):
            self._overall_hard_stopped = True

    def update_pnl(self, pnl: float, now: Optional[datetime] = None):
        """Update closed PnL."""
        if now is not None:
            self._ensure_day(now)
        self._closed_pnl_since_reset += pnl
        self._overall_pnl += pnl
        self._daily_pnl = self._floating_pnl + self._closed_pnl_since_reset

        if self._daily_pnl <= -self.DAILY_HARD_STOP:
            self._daily_hard_stopped = True
        if self._overall_pnl <= -(self.MAX_OVERALL_LOSS - self.OVERALL_BUFFER):
            self._overall_hard_stopped = True

    def get_risk_pct(self, now: Optional[datetime] = None) -> float:
        if now is not None:
            self._ensure_day(now)

        base_jittered = self._rng.uniform(self.RISK_JITTER_RANGE_MIN, self.RISK_JITTER_RANGE_MAX)
        risk = base_jittered

        if self._rng.random() < self.OUT_OF_BOUNDS_PROB:
            extra = self._rng.uniform(self.OUT_OF_BOUNDS_EXTRA_MIN, self.OUT_OF_BOUNDS_EXTRA_MAX)
            if self._rng.random() < 0.5:
                risk = self.RISK_JITTER_RANGE_MAX + extra
            else:
                risk = max(0.001, self.RISK_JITTER_RANGE_MIN - extra)

        risk = max(0.001, min(0.05, risk))
        return round(risk, 5)

    def get_risk_usd(self, equity: float, now: Optional[datetime] = None) -> float:
        pct = self.get_risk_pct(now=now)
        return round(equity * pct, 2)

    def get_sl_tp_profile(self) -> Dict[str, float]:
        idx = self._choose_profile_idx()
        prof = self._profiles[idx]
        return {
            "sl_mult": float(prof["sl_mult"]),
            "tp_mult": float(prof["tp_mult"]),
            "profile_id": f"profile_{idx}",
            "weight": float(prof["weight"]),
        }

    def get_lot_size(self, base_lot: float) -> float:
        if self._rng.random() < self.LOT_JITTER_PROB:
            direction = self._rng.choice([-1, 1])
            new_lot = base_lot + direction * self.LOT_STEP
            new_lot = max(self.LOT_STEP, new_lot)
            return round(new_lot, 2)
        return round(base_lot, 2)

    def get_share_size(self, base_shares: int) -> int:
        if self._rng.random() < self.LOT_JITTER_PROB:
            direction = self._rng.choice([-1, 1])
            new_shares = base_shares + direction
            new_shares = max(1, new_shares)
            return new_shares
        return base_shares

    def calculate_position_size(
        self,
        equity: float,
        risk_pct: float,
        entry: float,
        stop: float,
        point_value_lot: float = 100.0,
    ) -> float:
        if entry == stop:
            return self.LOT_STEP
        risk_cash = equity * risk_pct
        price_dist = abs(entry - stop)
        if price_dist <= 0:
            return self.LOT_STEP
        raw_lot = risk_cash / (price_dist * point_value_lot)
        steps = round(raw_lot / self.LOT_STEP)
        lot = max(self.LOT_STEP, steps * self.LOT_STEP)
        return self.get_lot_size(lot)

    def calculate_shares(
        self,
        risk_usd: float,
        entry: float,
        stop: float,
    ) -> int:
        sl_dist = abs(entry - stop)
        if sl_dist <= 0:
            return 1
        raw_shares = risk_usd / sl_dist
        shares = max(1, int(raw_shares))
        return self.get_share_size(shares)

    def is_daily_loss_limit_hit(self, daily_pnl: Optional[float] = None) -> bool:
        check = daily_pnl if daily_pnl is not None else self._daily_pnl
        return check <= -self.DAILY_LOSS_LIMIT

    def is_overall_loss_buffer_hit(self, overall_pnl: Optional[float] = None) -> bool:
        check = overall_pnl if overall_pnl is not None else self._overall_pnl
        return check <= -(self.MAX_OVERALL_LOSS - self.OVERALL_BUFFER)

    def is_daily_hard_stop_hit(self, daily_pnl: Optional[float] = None) -> bool:
        check = daily_pnl if daily_pnl is not None else self._daily_pnl
        return check <= -self.DAILY_HARD_STOP

    def can_trade(self, daily_pnl: Optional[float] = None, overall_pnl: Optional[float] = None, now: Optional[datetime] = None) -> tuple[bool, str]:
        if now is not None:
            self._ensure_day(now)
        dp = daily_pnl if daily_pnl is not None else self._daily_pnl
        op = overall_pnl if overall_pnl is not None else self._overall_pnl

        if self._overall_hard_stopped or op <= -(self.MAX_OVERALL_LOSS - self.OVERALL_BUFFER):
            return False, f"overall loss buffer hit: {op:.2f} <= -{self.MAX_OVERALL_LOSS - self.OVERALL_BUFFER}"
        if op <= -self.MAX_OVERALL_LOSS:
            return False, f"max overall loss hit: {op:.2f} <= -{self.MAX_OVERALL_LOSS}"
        if self._daily_hard_stopped or dp <= -self.DAILY_HARD_STOP:
            return False, f"daily hard stop hit: {dp:.2f} <= -{self.DAILY_HARD_STOP}"
        if dp <= -self.DAILY_LOSS_LIMIT:
            return False, f"daily loss limit hit: {dp:.2f} <= -{self.DAILY_LOSS_LIMIT}"
        return True, "OK"

    def should_force_close(self, floating_pnl: Optional[float] = None, daily_pnl: Optional[float] = None, overall_pnl: Optional[float] = None) -> tuple[bool, str]:
        """Check if we should force close positions due to approaching daily/overall limits."""
        dp = daily_pnl if daily_pnl is not None else self._daily_pnl
        op = overall_pnl if overall_pnl is not None else self._overall_pnl
        fp = floating_pnl if floating_pnl is not None else self._floating_pnl

        # If daily floating loss approaching -$30, force close
        if dp <= -self.DAILY_HARD_STOP:
            return True, f"force close: daily hard stop {dp:.2f} <= -{self.DAILY_HARD_STOP}"
        # If overall approaching -$90, force close
        if op <= -(self.MAX_OVERALL_LOSS - self.OVERALL_BUFFER):
            return True, f"force close: overall buffer {op:.2f} <= -{self.MAX_OVERALL_LOSS - self.OVERALL_BUFFER}"
        # Early warning: if daily loss within $5 of hard stop, consider closing
        if dp <= -(self.DAILY_HARD_STOP - 5):
            return True, f"force close early warning: daily {dp:.2f} within $5 of hard stop"
        return False, "OK"

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_overall_pnl(self) -> float:
        return self._overall_pnl

    def get_floating_pnl(self) -> float:
        return self._floating_pnl

    def reset_daily(self, now: Optional[datetime] = None):
        if now is None:
            self._current_day = None
            self._last_reset_date = None
        else:
            # Simulate reset window logic
            now_utc = now
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=timezone.utc)
            utc4 = now_utc + timedelta(hours=self._reset_offset)
            self._last_reset_date = None
            self._current_day = None
            self._ensure_day(now)
        self._daily_pnl = self._floating_pnl
        self._closed_pnl_since_reset = 0.0
        self._daily_hard_stopped = False

    def reset(self):
        self._last_profile_idx = None
        self._current_day = None
        self._daily_pnl = 0.0
        self._overall_pnl = 0.0
        self._floating_pnl = 0.0
        self._closed_pnl_since_reset = 0.0
        self._daily_hard_stopped = False
        self._overall_hard_stopped = False
        self._last_reset_date = None
        self._balance_at_day_start = self.STARTING_BALANCE
