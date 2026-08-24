"""StealthExecutionEngine — integrates 5 humanization modules.

Contract:
    process_signal(signal, now_utc, equity) -> execution_plan dict | None
    manage_position(position, now_utc) -> list[action dict]

6 gates sequential: session check → session end buffer → min gap → humanized delay → risk params → order hygiene.
If any gate fails → None (signal skipped).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from .config import StealthConfig
from .humanized_timer import HumanizedTimer
from .humanized_risk_manager import HumanizedRiskManager
from .session_simulator import SessionSimulator
from .order_hygiene import OrderHygiene
from .equity_curve_humanizer import EquityCurveHumanizer

logger = logging.getLogger("stealth_engine")


class StealthExecutionEngine:
    """Wraps existing strategy, making execution statistically human."""

    def __init__(
        self,
        news_calendar: Optional[List[Dict]] = None,
        config: Optional[StealthConfig] = None,
        seed: Optional[int] = None,
        timer: Optional[HumanizedTimer] = None,
        risk_manager: Optional[HumanizedRiskManager] = None,
        session_sim: Optional[SessionSimulator] = None,
        hygiene: Optional[OrderHygiene] = None,
        equity_humanizer: Optional[EquityCurveHumanizer] = None,
    ):
        # Config handling
        if config is None:
            config = StealthConfig(seed=seed)
        elif seed is not None:
            # Override seed if provided
            config.seed = seed

        self.config = config
        self.enabled = config.enabled

        # If news_calendar not provided, try to load from existing sources
        # TODO: connect to news/calendar_feed.py if available; currently empty and TODO
        if news_calendar is None:
            news_calendar = self._load_news_calendar()

        # Seed handling: use config.seed or provided seed, then derive per-module seeds
        base_seed = config.seed if config.seed is not None else seed
        # Derive different seeds for each module to avoid correlation but keep reproducibility
        def _derive(suffix: int) -> Optional[int]:
            if base_seed is None:
                return None
            return base_seed + suffix

        self.timer = timer or HumanizedTimer(
            news_calendar=news_calendar, seed=_derive(1), config=config
        )
        self.risk_manager = risk_manager or HumanizedRiskManager(
            risk_base=config.risk_base, seed=_derive(2), config=config
        )
        self.session_sim = session_sim or SessionSimulator(
            seed=_derive(3), config=config
        )
        self.hygiene = hygiene or OrderHygiene(seed=_derive(4), config=config)
        self.equity_humanizer = equity_humanizer or EquityCurveHumanizer(
            seed=_derive(5), config=config
        )

        # Keep news calendar reference for updates
        self.news_calendar = news_calendar

    def _load_news_calendar(self) -> List[Dict]:
        """Try to load from existing news calendar sources, else empty.

        TODO: integrate with news/calendar_feed.py or data/news_calendar_cache.json
        if available. Currently returns empty list with TODO.
        """
        # Attempt to load from news/calendar_feed.py
        try:
            from news.calendar_feed import get_feed

            feed = get_feed()
            events = feed.get_upcoming(hours=48)
            converted = []
            for ev in events:
                if ev.is_high:
                    # ev.datetime_utc is naive UTC
                    dt = ev.datetime_utc
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    converted.append({"time": dt, "impact": "high", "title": ev.title})
            if converted:
                logger.debug(f"Loaded {len(converted)} high-impact events from calendar_feed")
                return converted
        except Exception as e:
            logger.debug(f"Could not load calendar_feed: {e}")

        # Attempt to load from data/news_calendar_cache.json (legacy)
        try:
            import json
            import os
            from datetime import datetime

            cache_path = os.path.join("data", "news_calendar_cache.json")
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                converted = []
                for ev in data.get("events", []):
                    if ev.get("impact") == "High" and ev.get("country") in ("USD", "ALL"):
                        try:
                            dt = datetime.fromisoformat(ev["date"])
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            converted.append({"time": dt, "impact": "high", "title": ev.get("title", "")})
                        except Exception:
                            continue
                if converted:
                    logger.debug(f"Loaded {len(converted)} events from news_calendar_cache.json")
                    return converted
        except Exception as e:
            logger.debug(f"Could not load news_calendar_cache.json: {e}")

        # TODO: If no existing calendar source, leave empty and add TODO for future integration
        # The task says: Подключить к существующему источнику календаря в проекте если есть.
        # Если нет — оставить пустым и добавить TODO.
        logger.debug("No news calendar found, using empty calendar (TODO: integrate if source appears)")
        return []

    def update_news_calendar(self, news_calendar: List[Dict]):
        """Update news calendar at runtime."""
        self.news_calendar = news_calendar
        self.timer.news_calendar = news_calendar

    def process_signal(
        self,
        signal: Dict[str, Any],
        now_utc: datetime,
        equity: float,
    ) -> Optional[Dict[str, Any]]:
        """Process signal through 6 gates.

        Args:
            signal: dict from strategy (must have at least bias, entry, etc.)
            now_utc: current UTC datetime
            equity: current account equity

        Returns:
            execution_plan dict | None (None = skip signal)
        """
        if not self.enabled:
            # If disabled, pass through with minimal plan
            return {
                "delay_sec": 0.0,
                "risk_pct": self.config.risk_base,
                "sl_tp_profile": self.risk_manager.get_sl_tp_profile(),
                "magic": self.hygiene.get_next_magic(),
                "comment": self.hygiene.get_comment(),
                "api_jitter_ms": 0,
                "lot_jitter_applied": False,
            }

        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        # Gate 1: session check
        if not self.session_sim.is_in_trading_session(now_utc):
            logger.debug(f"Stealth gate 1 FAILED: outside trading session at {now_utc}")
            return None

        # Gate 2: session end buffer
        if self.session_sim.is_in_session_end_buffer(now_utc):
            logger.debug(f"Stealth gate 2 FAILED: session end buffer at {now_utc}")
            return None

        # Gate 3: min gap
        if not self.timer.is_min_gap_ok(now_utc):
            logger.debug(
                f"Stealth gate 3 FAILED: min gap not satisfied "
                f"(gap={self.timer.get_current_min_gap()}s, last={self.timer._last_order_time})"
            )
            return None

        # Gate 4: humanized delay
        delay_sec = self.timer.get_entry_delay(now_utc)

        # Gate 5: risk params
        risk_pct = self.risk_manager.get_risk_pct()
        sl_tp_profile = self.risk_manager.get_sl_tp_profile()

        # Calculate lot size if possible (needs entry/stop/equity)
        base_lot = signal.get("volume") or signal.get("lot") or 0.10
        try:
            # Try to compute lot from equity if signal has entry/invalidation
            entry = signal.get("entry_zone")
            if isinstance(entry, (list, tuple)) and len(entry) > 0:
                entry_price = float(entry[0])
            else:
                entry_price = float(signal.get("entry") or signal.get("price") or 0)
            stop = float(signal.get("invalidation") or signal.get("stop") or 0)
            if entry_price and stop:
                lot = self.risk_manager.calculate_position_size(
                    equity=equity,
                    risk_pct=risk_pct,
                    entry=entry_price,
                    stop=stop,
                )
            else:
                lot = self.risk_manager.get_lot_size(float(base_lot))
        except Exception:
            lot = self.risk_manager.get_lot_size(float(base_lot))

        # Gate 6: order hygiene
        magic = self.hygiene.get_next_magic()
        comment = self.hygiene.get_comment()
        api_jitter_ms = self.hygiene.get_api_jitter_ms()

        # Record order for state tracking (only after all gates passed)
        # Note: actual recording should happen after successful OrderSend,
        # but we record now to enforce gap for next signal; caller should also
        # call record_order on success.
        # We do NOT record yet here; caller will call engine.record_order_executed()
        # Instead, we provide a plan that includes a callback.

        execution_plan = {
            "delay_sec": delay_sec,
            "close_delay_sec": self.timer.get_close_delay(now_utc),
            "risk_pct": risk_pct,
            "sl_tp_profile": sl_tp_profile,
            "sl_mult": sl_tp_profile["sl_mult"],
            "tp_mult": sl_tp_profile["tp_mult"],
            "lot": lot,
            "base_lot": base_lot,
            "magic": magic,
            "comment": comment,
            "api_jitter_ms": api_jitter_ms,
            "api_jitter_sec": api_jitter_ms / 1000.0,
            "daily_cap": self.session_sim.get_daily_cap(),
            "orders_today": self.session_sim.get_orders_today(),
            "min_gap_sec": self.timer.get_current_min_gap(),
            "timestamp_utc": now_utc,
            "signal_id": signal.get("signal_id"),
        }

        logger.debug(f"Stealth execution plan: {execution_plan}")
        return execution_plan

    def record_order_executed(self, now_utc: datetime):
        """Call after successful OrderSend to update internal counters."""
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        self.timer.record_order(now_utc)
        self.session_sim.record_order(now_utc)

    def manage_position(self, position: Dict[str, Any], now_utc: datetime) -> List[Dict[str, Any]]:
        """Delegate to EquityCurveHumanizer, adding close delays.

        Args:
            position: dict with entry_price, current_price, stop_price, tp_price, side, etc.
            now_utc: current UTC datetime

        Returns:
            list of action dicts with added delay
        """
        if not self.enabled:
            return []

        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        actions = self.equity_humanizer.manage_position(position)
        # Enrich with humanized close delay and api jitter
        for act in actions:
            act["delay_sec"] = self.timer.get_close_delay(now_utc)
            act["api_jitter_ms"] = self.hygiene.get_api_jitter_ms()
            act["api_jitter_sec"] = act["api_jitter_ms"] / 1000.0
            act["timestamp_utc"] = now_utc
            # Add magic/comment for new orders if needed
            if act["type"] in ("partial_exit",):
                act["magic"] = self.hygiene.get_next_magic()
                act["comment"] = self.hygiene.get_comment()

        return actions

    def reset_daily(self, now_utc: Optional[datetime] = None):
        """Reset daily counters (for testing or manual reset)."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        # Force new day logic via internal methods
        self.session_sim.force_new_day(now_utc)
        self.timer._ensure_day(now_utc)

    def get_state(self) -> Dict[str, Any]:
        """Return current internal state for diagnostics."""
        return {
            "daily_cap": self.session_sim.get_daily_cap(),
            "orders_today": self.session_sim.get_orders_today(),
            "is_no_trade_day": self.session_sim.is_no_trade_day_today(),
            "session_end_buffer_sec": self.session_sim.get_session_end_buffer_sec(),
            "min_gap_sec": self.timer.get_current_min_gap(),
            "last_order_time": self.timer._last_order_time,
            "magic_pool": self.hygiene.get_magic_pool(),
            "last_magic": self.hygiene._last_magic,
        }
