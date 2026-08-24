"""StealthExecutionEngine — integrates 5 humanization modules for both MT5 and UTEx.

New contract for UTEx challenge:
    process_signal(signal, now_et, equity, daily_pnl, overall_pnl) -> execution_plan dict | None
    manage_position(position, now_et) -> list[action dict]

Gates for challenge: session check → session end buffer → daily loss check → overall loss buffer check → min gap → humanized delay → risk params → browser action plan.
If any gate fails → None.
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

# BrowserHumanizer is optional import (may need playwright)
try:
    from .browser_humanizer import BrowserHumanizer
except ImportError:
    BrowserHumanizer = None

logger = logging.getLogger("stealth_engine")


class StealthExecutionEngine:
    """Wraps existing strategy, making execution statistically human."""

    def __init__(
        self,
        news_calendar: Optional[List[Dict]] = None,
        earnings_calendar: Optional[List[Dict]] = None,
        config: Optional[StealthConfig] = None,
        seed: Optional[int] = None,
        timer: Optional[HumanizedTimer] = None,
        risk_manager: Optional[HumanizedRiskManager] = None,
        session_sim: Optional[SessionSimulator] = None,
        hygiene: Optional[OrderHygiene] = None,
        equity_humanizer: Optional[EquityCurveHumanizer] = None,
        browser_humanizer: Optional[Any] = None,
        page: Optional[Any] = None,
        use_et: bool = False,
    ):
        if config is None:
            config = StealthConfig(seed=seed)
        elif seed is not None:
            config.seed = seed

        self.config = config
        self.enabled = config.enabled
        self.use_et = use_et or getattr(config, "use_et", False)

        # Load calendars if not provided
        if news_calendar is None or earnings_calendar is None:
            loaded_news, loaded_earnings = self._load_calendars()
            if news_calendar is None:
                news_calendar = loaded_news
            if earnings_calendar is None:
                earnings_calendar = loaded_earnings

        base_seed = config.seed if config.seed is not None else seed

        def _derive(suffix: int) -> Optional[int]:
            if base_seed is None:
                return None
            return base_seed + suffix

        self.timer = timer or HumanizedTimer(
            news_calendar=news_calendar,
            earnings_calendar=earnings_calendar,
            seed=_derive(1),
            config=config,
        )
        self.risk_manager = risk_manager or HumanizedRiskManager(
            risk_base=config.risk_base, seed=_derive(2), config=config
        )
        self.session_sim = session_sim or SessionSimulator(
            seed=_derive(3), config=config, use_et=self.use_et
        )
        self.hygiene = hygiene or OrderHygiene(seed=_derive(4), config=config)
        self.equity_humanizer = equity_humanizer or EquityCurveHumanizer(
            seed=_derive(5), config=config
        )

        if BrowserHumanizer is not None:
            self.browser_humanizer = browser_humanizer or BrowserHumanizer(
                page=page, seed=_derive(6), config=config
            )
        else:
            self.browser_humanizer = browser_humanizer

        self.news_calendar = news_calendar
        self.earnings_calendar = earnings_calendar

    def _load_calendars(self) -> tuple[List[Dict], List[Dict]]:
        """Load news and earnings calendars from existing sources."""
        news_list: List[Dict] = []
        earnings_list: List[Dict] = []

        # News from news/calendar_feed.py
        try:
            from news.calendar_feed import get_feed

            feed = get_feed()
            events = feed.get_upcoming(hours=48)
            for ev in events:
                if ev.is_high:
                    dt = ev.datetime_utc
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    news_list.append({"time": dt, "impact": "high", "title": ev.title})
            if news_list:
                logger.debug(f"Loaded {len(news_list)} high-impact events from calendar_feed")
        except Exception as e:
            logger.debug(f"Could not load calendar_feed: {e}")

        # Legacy cache
        try:
            import json
            import os

            cache_path = os.path.join("data", "news_calendar_cache.json")
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for ev in data.get("events", []):
                    if ev.get("impact") == "High" and ev.get("country") in ("USD", "ALL"):
                        try:
                            dt = datetime.fromisoformat(ev["date"])
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            news_list.append({"time": dt, "impact": "high", "title": ev.get("title", "")})
                        except Exception:
                            continue
        except Exception as e:
            logger.debug(f"Could not load news_calendar_cache.json: {e}")

        # Earnings calendar - try challenge/manual/earnings_calendar.yaml
        try:
            import os
            import yaml

            earnings_path = os.path.join("challenge", "manual", "earnings_calendar.yaml")
            if os.path.exists(earnings_path):
                with open(earnings_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                # Expect dict or list
                if isinstance(data, dict):
                    for ticker, dates in data.items():
                        if isinstance(dates, list):
                            for d in dates:
                                earnings_list.append({"ticker": ticker, "date": d})
                        else:
                            earnings_list.append({"ticker": ticker, "date": dates})
                elif isinstance(data, list):
                    earnings_list.extend(data)
                if earnings_list:
                    logger.debug(f"Loaded {len(earnings_list)} earnings events")
        except Exception as e:
            logger.debug(f"Could not load earnings_calendar.yaml: {e}")

        # TODO: If no existing calendar source, leave empty and add TODO for future integration
        if not news_list:
            logger.debug("No news calendar found, using empty calendar (TODO: integrate if source appears)")
        if not earnings_list:
            logger.debug("No earnings calendar found, using empty calendar (TODO: integrate if source appears)")

        return news_list, earnings_list

    def update_news_calendar(self, news_calendar: List[Dict]):
        self.news_calendar = news_calendar
        self.timer.news_calendar = news_calendar

    def update_earnings_calendar(self, earnings_calendar: List[Dict]):
        self.earnings_calendar = earnings_calendar
        self.timer.update_earnings_calendar(earnings_calendar)

    def process_signal(
        self,
        signal: Dict[str, Any],
        now_utc: datetime,
        equity: float,
        daily_pnl: Optional[float] = None,
        overall_pnl: Optional[float] = None,
        floating_pnl: Optional[float] = None,
        *args,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """Process signal through gates.

        New contract: process_signal(signal, now_et, equity, floating_pnl, daily_pnl, overall_pnl)
        For MT5: daily_pnl/overall_pnl/floating_pnl can be None (uses internal tracking).

        Gates: session check → session end buffer → daily floating loss check (-$30) → overall floating loss check (-$90) → min gap → humanized delay → risk params → browser action plan
        """
        # Backward compat: allow floating_pnl via kwargs or as daily_pnl if passed positionally in old way
        if floating_pnl is None:
            floating_pnl = kwargs.get("floating_pnl")
        if daily_pnl is None:
            daily_pnl = kwargs.get("daily_pnl")
        if overall_pnl is None:
            overall_pnl = kwargs.get("overall_pnl")
        if not self.enabled:
            return {
                "delay_sec": 0.0,
                "risk_pct": self.config.risk_base,
                "sl_tp_profile": self.risk_manager.get_sl_tp_profile(),
                "magic": self.hygiene.get_next_magic() if self.hygiene else 0,
                "comment": self.hygiene.get_comment() if self.hygiene else "",
                "api_jitter_ms": 0,
                "lot_jitter_applied": False,
                "browser_action": "click_dom",
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

        # Gate 3: daily loss check (challenge)
        if daily_pnl is not None or overall_pnl is not None:
            # Use provided PnL
            dp = daily_pnl if daily_pnl is not None else self.risk_manager.get_daily_pnl()
            op = overall_pnl if overall_pnl is not None else self.risk_manager.get_overall_pnl()
            can_trade, reason = self.risk_manager.can_trade(daily_pnl=dp, overall_pnl=op, now=now_utc)
            if not can_trade:
                logger.debug(f"Stealth gate 3/4 FAILED: {reason}")
                return None
        else:
            # For MT5 path, use internal risk manager's daily hard stop if tracked
            # If internal daily hard stopped, block
            if self.risk_manager._daily_hard_stopped or self.risk_manager._overall_hard_stopped:
                logger.debug(f"Stealth gate 3/4 FAILED: internal hard stop")
                return None

        # Earnings filter: if signal has ticker and it's earnings day, skip
        ticker = signal.get("ticker") or signal.get("symbol")
        if ticker:
            try:
                if self.timer.is_earnings_day(ticker, now_utc.date()):
                    logger.debug(f"Stealth gate EARNINGS FAILED: {ticker} earnings today {now_utc.date()}")
                    return None
            except Exception:
                pass

        # Gate 5: min gap
        if not self.timer.is_min_gap_ok(now_utc):
            logger.debug(
                f"Stealth gate 5 FAILED: min gap not satisfied "
                f"(gap={self.timer.get_current_min_gap()}s, last={self.timer._last_order_time})"
            )
            return None

        # Gate 6: humanized delay
        delay_sec = self.timer.get_entry_delay(now_utc)

        # Gate 7: risk params
        risk_pct = self.risk_manager.get_risk_pct(now=now_utc)
        sl_tp_profile = self.risk_manager.get_sl_tp_profile()

        # Position sizing: shares for challenge, lots for MT5
        base_lot = signal.get("volume") or signal.get("lot") or signal.get("qty") or 0.10
        lot = base_lot
        shares = None
        try:
            entry = signal.get("entry_zone")
            if isinstance(entry, (list, tuple)) and len(entry) > 0:
                entry_price = float(entry[0])
            else:
                entry_price = float(signal.get("entry") or signal.get("price") or 0)
            stop = float(signal.get("invalidation") or signal.get("stop") or 0)

            if entry_price and stop:
                # If challenge (shares)
                if signal.get("qty") or signal.get("ticker") in (self.config.challenge_tickers or []):
                    # Calculate risk USD: equity * risk_pct or fixed $10
                    risk_usd = equity * risk_pct if equity else 10.0
                    # For challenge, risk is $10 base with jitter 0.7-1.3%
                    # If equity is $1000, 1% = $10, matches spec
                    raw_shares = risk_usd / abs(entry_price - stop) if abs(entry_price - stop) > 0 else 1
                    shares = max(1, int(raw_shares))
                    shares = self.risk_manager.get_share_size(shares)
                    lot = shares
                else:
                    # MT5 lots
                    point_value = signal.get("point_value_lot", 100.0)
                    lot = self.risk_manager.calculate_position_size(
                        equity=equity,
                        risk_pct=risk_pct,
                        entry=entry_price,
                        stop=stop,
                        point_value_lot=point_value,
                    )
            else:
                if isinstance(base_lot, int) or (isinstance(base_lot, float) and base_lot.is_integer() and base_lot < 100):
                    # Likely shares
                    shares = self.risk_manager.get_share_size(int(base_lot))
                    lot = shares
                else:
                    lot = self.risk_manager.get_lot_size(float(base_lot))
        except Exception as e:
            logger.debug(f"Risk calc fallback: {e}")
            try:
                lot = self.risk_manager.get_lot_size(float(base_lot))
            except Exception:
                lot = base_lot

        # Gate 8: order hygiene / browser action plan
        magic = self.hygiene.get_next_magic() if self.hygiene else 0
        comment = self.hygiene.get_comment() if self.hygiene else ""
        api_jitter_ms = self.hygiene.get_api_jitter_ms() if self.hygiene else 0

        # Browser action variance: 70% DOM click, 30% hotkeys
        browser_action = "click_dom"
        if self.browser_humanizer is not None:
            # Use config prob
            if self.browser_humanizer._rng.random() < self.config.browser_action_click_dom_prob:
                browser_action = "click_dom"
            else:
                browser_action = "hotkey"
        else:
            # Fallback rng
            import random

            if random.random() < 0.70:
                browser_action = "click_dom"
            else:
                browser_action = "hotkey"

        execution_plan = {
            "delay_sec": delay_sec,
            "close_delay_sec": self.timer.get_close_delay(now_utc),
            "risk_pct": risk_pct,
            "risk_usd": equity * risk_pct if equity else 10.0,
            "sl_tp_profile": sl_tp_profile,
            "sl_mult": sl_tp_profile["sl_mult"],
            "tp_mult": sl_tp_profile["tp_mult"],
            "lot": lot,
            "shares": shares if shares is not None else lot,
            "base_lot": base_lot,
            "magic": magic,
            "comment": comment,
            "api_jitter_ms": api_jitter_ms,
            "api_jitter_sec": api_jitter_ms / 1000.0,
            "browser_action": browser_action,
            "daily_cap": self.session_sim.get_daily_cap(),
            "orders_today": self.session_sim.get_orders_today(),
            "min_gap_sec": self.timer.get_current_min_gap(),
            "timestamp_utc": now_utc,
            "signal_id": signal.get("signal_id"),
            "ticker": ticker,
        }

        logger.debug(f"Stealth execution plan: {execution_plan}")
        return execution_plan

    def record_order_executed(self, now_utc: datetime):
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        self.timer.record_order(now_utc)
        self.session_sim.record_order(now_utc)

    def manage_position(self, position: Dict[str, Any], now_utc: datetime, floating_pnl: Optional[float] = None, *args, **kwargs) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []

        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        actions = self.equity_humanizer.manage_position(position)
        for act in actions:
            act["delay_sec"] = self.timer.get_close_delay(now_utc)
            act["api_jitter_ms"] = self.hygiene.get_api_jitter_ms() if self.hygiene else 0
            act["api_jitter_sec"] = act["api_jitter_ms"] / 1000.0
            act["timestamp_utc"] = now_utc
            # Browser action for each close
            if self.browser_humanizer is not None:
                if self.browser_humanizer._rng.random() < self.config.browser_action_click_dom_prob:
                    act["browser_action"] = "click_dom"
                else:
                    act["browser_action"] = "hotkey"
            else:
                act["browser_action"] = "click_dom"

            if act["type"] in ("partial_exit",):
                act["magic"] = self.hygiene.get_next_magic() if self.hygiene else 0
                act["comment"] = self.hygiene.get_comment() if self.hygiene else ""

        return actions

    def reset_daily(self, now_utc: Optional[datetime] = None):
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        self.session_sim.force_new_day(now_utc)
        self.timer._ensure_day(now_utc)
        self.risk_manager.reset_daily(now=now_utc)

    def get_state(self) -> Dict[str, Any]:
        return {
            "daily_cap": self.session_sim.get_daily_cap(),
            "orders_today": self.session_sim.get_orders_today(),
            "is_no_trade_day": self.session_sim.is_no_trade_day_today(),
            "session_end_buffer_sec": self.session_sim.get_session_end_buffer_sec(),
            "min_gap_sec": self.timer.get_current_min_gap(),
            "last_order_time": self.timer._last_order_time,
            "magic_pool": self.hygiene.get_magic_pool() if self.hygiene else [],
            "last_magic": self.hygiene._last_magic if self.hygiene else None,
            "daily_pnl": self.risk_manager.get_daily_pnl(),
            "overall_pnl": self.risk_manager.get_overall_pnl(),
            "trading_days_count": self.session_sim.get_trading_days_count(),
            "browser_fingerprint": self.browser_humanizer.get_fingerprint_config() if self.browser_humanizer else None,
        }
