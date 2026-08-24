"""Stealth config with overridable defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple, List, Dict, Any, Optional


@dataclass
class StealthConfig:
    """Central config for all stealth modules.

    All timing/risk values live inside respective classes; this config only
    provides override points without hardcoding outside modules.
    """

    # Global toggle
    enabled: bool = True

    # HumanizedTimer overrides
    timer_base_reaction_range: Tuple[float, float] = (2.5, 8.0)
    timer_execution_jitter_range: Tuple[float, float] = (0.1, 1.5)
    timer_news_extra_range: Tuple[float, float] = (15.0, 90.0)
    timer_news_window_sec: int = 120
    timer_fatigue_drift_per_order: float = 0.3
    timer_fatigue_drift_max: float = 5.0
    timer_hesitation_prob: float = 0.08
    timer_hesitation_range: Tuple[float, float] = (5.0, 20.0)
    timer_min_gap_range: Tuple[int, int] = (180, 900)  # 3-15 min in sec
    timer_close_delay_range: Tuple[float, float] = (1.0, 5.0)

    # HumanizedRiskManager overrides
    risk_base: float = 0.01
    risk_jitter: float = 0.0035
    risk_out_of_bounds_prob: float = 0.05
    risk_out_of_bounds_extra: Tuple[float, float] = (0.001, 0.005)
    risk_lot_step: float = 0.01
    risk_lot_jitter_prob: float = 0.15
    # 6 weighted profiles (sl_mult, tp_mult, weight)
    risk_profiles: List[Dict[str, float]] = field(default_factory=lambda: [
        {"sl_mult": 1.0, "tp_mult": 1.5, "weight": 0.25},
        {"sl_mult": 1.0, "tp_mult": 1.8, "weight": 0.20},
        {"sl_mult": 1.0, "tp_mult": 2.0, "weight": 0.20},
        {"sl_mult": 1.1, "tp_mult": 1.8, "weight": 0.15},
        {"sl_mult": 1.2, "tp_mult": 2.0, "weight": 0.10},
        {"sl_mult": 1.2, "tp_mult": 2.2, "weight": 0.10},
    ])
    risk_no_repeat_prob: float = 0.70

    # SessionSimulator overrides
    london_window: Tuple[str, str] = ("07:30", "16:00")
    ny_window: Tuple[str, str] = ("08:00", "17:30")
    # breaks: list of (start, end) strings in HH:MM, applied to both sessions
    session_breaks: List[Tuple[str, str]] = field(default_factory=lambda: [
        ("12:00", "12:30"),
    ])
    daily_cap_range: Tuple[int, int] = (3, 7)
    no_trade_day_prob: float = 0.08
    session_end_buffer_range: Tuple[int, int] = (600, 1800)  # 10-30 min sec
    weekend_off: bool = True

    # OrderHygiene overrides
    magic_pool_size: int = 20
    magic_banned_ranges: List[Tuple[int, int]] = field(default_factory=lambda: [
        (0, 100),
        (70000000, 89000000),
    ])
    magic_allowed_ranges: List[Tuple[int, int]] = field(default_factory=lambda: [
        (101, 69999999),
        (89000001, 99999999),
    ])
    comment_empty_prob: float = 0.70
    comment_pool: List[str] = field(default_factory=lambda: [
        "xau",
        "gold long",
        "scalp",
        "news play",
    ])
    api_jitter_range_ms: Tuple[int, int] = (50, 350)

    # EquityCurveHumanizer overrides
    partial_exit_prob: float = 0.25
    partial_exit_pct_range: Tuple[float, float] = (0.30, 0.50)
    partial_exit_trigger_r: float = 1.0
    early_close_prob: float = 0.12
    early_close_trigger_tp_ratio: float = 0.6
    trailing_start_r: float = 1.5
    trailing_pips_range: Tuple[int, int] = (15, 40)
    pip_value: float = 0.1  # XAUUSD: 1 pip = 0.1 price unit

    # Misc
    seed: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StealthConfig":
        """Create config from dict, ignoring unknown keys."""
        if not d:
            return cls()
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        # Convert list tuples back to tuples where needed
        for k in [
            "timer_base_reaction_range",
            "timer_execution_jitter_range",
            "timer_news_extra_range",
            "timer_hesitation_range",
            "timer_min_gap_range",
            "timer_close_delay_range",
            "risk_out_of_bounds_extra",
            "london_window",
            "ny_window",
            "daily_cap_range",
            "session_end_buffer_range",
            "api_jitter_range_ms",
            "partial_exit_pct_range",
            "trailing_pips_range",
        ]:
            if k in filtered and isinstance(filtered[k], list):
                filtered[k] = tuple(filtered[k])
        # session_breaks list of tuples
        if "session_breaks" in filtered:
            filtered["session_breaks"] = [tuple(x) for x in filtered["session_breaks"]]
        if "magic_banned_ranges" in filtered:
            filtered["magic_banned_ranges"] = [tuple(x) for x in filtered["magic_banned_ranges"]]
        if "magic_allowed_ranges" in filtered:
            filtered["magic_allowed_ranges"] = [tuple(x) for x in filtered["magic_allowed_ranges"]]
        return cls(**filtered)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "timer_base_reaction_range": self.timer_base_reaction_range,
            "timer_execution_jitter_range": self.timer_execution_jitter_range,
            "timer_news_extra_range": self.timer_news_extra_range,
            "timer_news_window_sec": self.timer_news_window_sec,
            "timer_fatigue_drift_per_order": self.timer_fatigue_drift_per_order,
            "timer_fatigue_drift_max": self.timer_fatigue_drift_max,
            "timer_hesitation_prob": self.timer_hesitation_prob,
            "timer_hesitation_range": self.timer_hesitation_range,
            "timer_min_gap_range": self.timer_min_gap_range,
            "timer_close_delay_range": self.timer_close_delay_range,
            "risk_base": self.risk_base,
            "risk_jitter": self.risk_jitter,
            "risk_out_of_bounds_prob": self.risk_out_of_bounds_prob,
            "risk_out_of_bounds_extra": self.risk_out_of_bounds_extra,
            "risk_lot_step": self.risk_lot_step,
            "risk_lot_jitter_prob": self.risk_lot_jitter_prob,
            "risk_profiles": self.risk_profiles,
            "risk_no_repeat_prob": self.risk_no_repeat_prob,
            "london_window": self.london_window,
            "ny_window": self.ny_window,
            "session_breaks": self.session_breaks,
            "daily_cap_range": self.daily_cap_range,
            "no_trade_day_prob": self.no_trade_day_prob,
            "session_end_buffer_range": self.session_end_buffer_range,
            "weekend_off": self.weekend_off,
            "magic_pool_size": self.magic_pool_size,
            "magic_banned_ranges": self.magic_banned_ranges,
            "magic_allowed_ranges": self.magic_allowed_ranges,
            "comment_empty_prob": self.comment_empty_prob,
            "comment_pool": self.comment_pool,
            "api_jitter_range_ms": self.api_jitter_range_ms,
            "partial_exit_prob": self.partial_exit_prob,
            "partial_exit_pct_range": self.partial_exit_pct_range,
            "partial_exit_trigger_r": self.partial_exit_trigger_r,
            "early_close_prob": self.early_close_prob,
            "early_close_trigger_tp_ratio": self.early_close_trigger_tp_ratio,
            "trailing_start_r": self.trailing_start_r,
            "trailing_pips_range": self.trailing_pips_range,
            "pip_value": self.pip_value,
            "seed": self.seed,
        }
