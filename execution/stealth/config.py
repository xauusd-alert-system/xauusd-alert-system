"""Stealth config with overridable defaults for both MT5 and UTEx challenge."""

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
    risk_jitter_range: Tuple[float, float] = (0.007, 0.013)  # 0.7-1.3% for challenge
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
    # Challenge limits
    challenge_starting_balance: float = 1000.0
    challenge_profit_target: float = 80.0
    challenge_daily_loss_limit: float = 50.0
    challenge_max_overall_loss: float = 100.0
    challenge_starting_loss: float = 2.90
    challenge_daily_hard_stop: float = 30.0  # -$30 floating hard stop, before platform -$50
    challenge_overall_buffer: float = 10.0  # buffer $10 before overall -> stop at -$90 floating
    challenge_min_trading_days: int = 5
    challenge_tickers: List[str] = field(default_factory=lambda: ["TSLA", "AAPL", "NVDA", "AMZN", "META"])
    # Daily reset window in UTC+4: 00:00-00:13 UTC+4 = 20:00-20:13 UTC
    challenge_daily_reset_window_utc4: Tuple[str, str] = ("00:00", "00:13")
    challenge_daily_reset_offset_hours: int = 4  # UTC+4
    # UTEx hotkeys: F1-F4, F9-F10, Shift+F1-F4 customizable
    browser_hotkey_map: Dict[str, str] = field(default_factory=lambda: {
        "buy_market_best_ask": "F1",
        "sell_market_best_bid": "F2",
        "buy_limit_best_bid": "F3",
        "sell_limit_best_ask": "F4",
        "buy_stop_mark": "F9",
        "sell_stop_mark": "F10",
        "buy_market_mark": "Shift+F1",
        "sell_market_mark": "Shift+F2",
        "close_position": "Shift+F3",
        "cancel_all": "Shift+F4",
    })

    # SessionSimulator overrides
    london_window: Tuple[str, str] = ("07:30", "16:00")
    ny_window: Tuple[str, str] = ("08:00", "17:30")
    # ET windows for UTEx challenge
    et_range_window: Tuple[str, str] = ("09:30", "09:45")  # ORB range
    et_entry_window: Tuple[str, str] = ("09:45", "10:30")  # entry
    et_close_all_time: str = "15:30"
    et_tab_open_window: Tuple[str, str] = ("09:20", "09:28")
    et_wind_down_window: Tuple[str, str] = ("10:30", "11:00")
    # breaks: list of (start, end) strings in HH:MM, applied to both sessions
    session_breaks: List[Tuple[str, str]] = field(default_factory=lambda: [
        ("12:00", "12:30"),
    ])
    daily_cap_range: Tuple[int, int] = (3, 7)
    challenge_daily_cap: int = 2
    no_trade_day_prob: float = 0.08
    session_end_buffer_range: Tuple[int, int] = (600, 1800)  # 10-30 min sec
    weekend_off: bool = True
    market_holidays: List[str] = field(default_factory=lambda: [
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
    ])

    # OrderHygiene overrides (MT5)
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

    # BrowserHumanizer overrides (UTEx challenge)
    browser_headless: bool = False
    browser_viewport: Tuple[int, int] = (1920, 1080)
    browser_user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    browser_timezone: str = "America/New_York"
    browser_mouse_bezier_steps: Tuple[int, int] = (20, 40)
    browser_mouse_jitter_px: Tuple[int, int] = (1, 3)
    browser_visibility_switches_per_session: Tuple[int, int] = (2, 3)
    browser_visibility_duration_range: Tuple[int, int] = (30, 120)
    browser_action_click_dom_prob: float = 0.70
    browser_action_hotkey_prob: float = 0.30
    browser_idle_break_interval_range: Tuple[int, int] = (480, 900)  # 8-15 min sec
    browser_idle_break_duration_range: Tuple[int, int] = (20, 60)

    # EquityCurveHumanizer overrides
    partial_exit_prob: float = 0.25
    partial_exit_pct_range: Tuple[float, float] = (0.30, 0.50)
    partial_exit_trigger_r: float = 1.0
    early_close_prob: float = 0.12
    early_close_trigger_tp_ratio: float = 0.6
    trailing_start_r: float = 1.5
    trailing_pips_range: Tuple[int, int] = (15, 40)
    trailing_dollars_range: Tuple[float, float] = (0.50, 2.00)
    pip_value: float = 0.1  # XAUUSD: 1 pip = 0.1 price unit

    # Mode
    use_et: bool = False  # True for UTEx challenge ET windows, False for MT5 London/NY

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
        tuple_keys = [
            "timer_base_reaction_range",
            "timer_execution_jitter_range",
            "timer_news_extra_range",
            "timer_hesitation_range",
            "timer_min_gap_range",
            "timer_close_delay_range",
            "risk_out_of_bounds_extra",
            "risk_jitter_range",
            "london_window",
            "ny_window",
            "et_range_window",
            "et_entry_window",
            "et_tab_open_window",
            "et_wind_down_window",
            "daily_cap_range",
            "session_end_buffer_range",
            "api_jitter_range_ms",
            "partial_exit_pct_range",
            "trailing_pips_range",
            "trailing_dollars_range",
            "browser_viewport",
            "browser_mouse_bezier_steps",
            "browser_mouse_jitter_px",
            "browser_visibility_switches_per_session",
            "browser_visibility_duration_range",
            "browser_idle_break_interval_range",
            "browser_idle_break_duration_range",
            "challenge_daily_reset_window_utc4",
        ]
        for k in tuple_keys:
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
            "risk_jitter_range": self.risk_jitter_range,
            "risk_lot_step": self.risk_lot_step,
            "risk_lot_jitter_prob": self.risk_lot_jitter_prob,
            "risk_profiles": self.risk_profiles,
            "risk_no_repeat_prob": self.risk_no_repeat_prob,
            "challenge_starting_balance": self.challenge_starting_balance,
            "challenge_profit_target": self.challenge_profit_target,
            "challenge_daily_loss_limit": self.challenge_daily_loss_limit,
            "challenge_max_overall_loss": self.challenge_max_overall_loss,
            "challenge_starting_loss": self.challenge_starting_loss,
            "challenge_daily_hard_stop": self.challenge_daily_hard_stop,
            "challenge_overall_buffer": self.challenge_overall_buffer,
            "challenge_min_trading_days": self.challenge_min_trading_days,
            "challenge_tickers": self.challenge_tickers,
            "challenge_daily_reset_window_utc4": self.challenge_daily_reset_window_utc4,
            "challenge_daily_reset_offset_hours": self.challenge_daily_reset_offset_hours,
            "browser_hotkey_map": self.browser_hotkey_map,
            "use_et": self.use_et,
            "london_window": self.london_window,
            "ny_window": self.ny_window,
            "et_range_window": self.et_range_window,
            "et_entry_window": self.et_entry_window,
            "et_close_all_time": self.et_close_all_time,
            "et_tab_open_window": self.et_tab_open_window,
            "et_wind_down_window": self.et_wind_down_window,
            "session_breaks": self.session_breaks,
            "daily_cap_range": self.daily_cap_range,
            "challenge_daily_cap": self.challenge_daily_cap,
            "no_trade_day_prob": self.no_trade_day_prob,
            "session_end_buffer_range": self.session_end_buffer_range,
            "weekend_off": self.weekend_off,
            "market_holidays": self.market_holidays,
            "magic_pool_size": self.magic_pool_size,
            "magic_banned_ranges": self.magic_banned_ranges,
            "magic_allowed_ranges": self.magic_allowed_ranges,
            "comment_empty_prob": self.comment_empty_prob,
            "comment_pool": self.comment_pool,
            "api_jitter_range_ms": self.api_jitter_range_ms,
            "browser_headless": self.browser_headless,
            "browser_viewport": self.browser_viewport,
            "browser_user_agent": self.browser_user_agent,
            "browser_timezone": self.browser_timezone,
            "browser_mouse_bezier_steps": self.browser_mouse_bezier_steps,
            "browser_mouse_jitter_px": self.browser_mouse_jitter_px,
            "browser_visibility_switches_per_session": self.browser_visibility_switches_per_session,
            "browser_visibility_duration_range": self.browser_visibility_duration_range,
            "browser_action_click_dom_prob": self.browser_action_click_dom_prob,
            "browser_action_hotkey_prob": self.browser_action_hotkey_prob,
            "browser_idle_break_interval_range": self.browser_idle_break_interval_range,
            "browser_idle_break_duration_range": self.browser_idle_break_duration_range,
            "partial_exit_prob": self.partial_exit_prob,
            "partial_exit_pct_range": self.partial_exit_pct_range,
            "partial_exit_trigger_r": self.partial_exit_trigger_r,
            "early_close_prob": self.early_close_prob,
            "early_close_trigger_tp_ratio": self.early_close_trigger_tp_ratio,
            "trailing_start_r": self.trailing_start_r,
            "trailing_pips_range": self.trailing_pips_range,
            "trailing_dollars_range": self.trailing_dollars_range,
            "pip_value": self.pip_value,
            "seed": self.seed,
        }
