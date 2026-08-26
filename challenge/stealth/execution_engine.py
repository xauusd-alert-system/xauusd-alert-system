"""StealthExecutionEngine — integration layer wrapping the ORB strategy.

Sequential gates:
    session → session-end buffer → daily floating loss (-$30) →
    overall floating loss (-$90) → earnings skip → min gap →
    humanized delay → risk params → browser action plan.

Contract:
    process_signal(signal, now_et, equity, floating_pnl, daily_pnl, overall_pnl)
        -> execution_plan dict | None
    manage_position(position, now_et, floating_pnl)
        -> list[action dict]
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from challenge.stealth.humanized_timer import HumanizedTimer
from challenge.stealth.humanized_risk_manager import HumanizedRiskManager
from challenge.stealth.session_simulator import SessionSimulator
from challenge.stealth.browser_humanizer import BrowserHumanizer
from challenge.stealth.equity_curve_humanizer import EquityCurveHumanizer
from challenge.orb_strategy import ORBSignal

logger = logging.getLogger("stealth.engine")


class StealthExecutionEngine:
    """Wraps the ORB strategy with stealth execution gates.

    Parameters
    ----------
    cfg : dict
        Challenge config section.
    timer : HumanizedTimer | None
    risk : HumanizedRiskManager | None
    session : SessionSimulator | None
    humanizer : BrowserHumanizer | None
    equity_humanizer : EquityCurveHumanizer | None
    """

    def __init__(
        self,
        cfg: dict,
        *,
        timer: Optional[HumanizedTimer] = None,
        risk: Optional[HumanizedRiskManager] = None,
        session: Optional[SessionSimulator] = None,
        humanizer: Optional[BrowserHumanizer] = None,
        equity_humanizer: Optional[EquityCurveHumanizer] = None,
        seed: Optional[int] = None,
    ) -> None:
        self._cfg = cfg
        self._seed = seed

        # Create defaults if not provided
        stealth_cfg = cfg.get("stealth", {})
        challenge_cfg = cfg.get("challenge", {})

        self.timer = timer or HumanizedTimer(
            seed=seed,
            news_calendar=stealth_cfg.get("news_calendar"),
            earnings_calendar=stealth_cfg.get("earnings_calendar"),
        )
        self.risk = risk or HumanizedRiskManager(
            start_balance=challenge_cfg.get("risk", {}).get("start_balance", 1000.0),
            risk_base_pct=challenge_cfg.get("stealth", {}).get("risk_base_pct", 0.01),
            seed=seed,
            cfg=challenge_cfg.get("risk"),
        )
        self.session = session or SessionSimulator(
            seed=seed,
            use_et=True,
            cfg=challenge_cfg.get("session"),
        )
        self.humanizer = humanizer or BrowserHumanizer(
            seed=seed,
            cfg=challenge_cfg.get("browser"),
        )
        self.equity_humanizer = equity_humanizer or EquityCurveHumanizer(
            seed=seed,
            cfg=stealth_cfg.get("equity_curve"),
        )

        # Tracking
        self._last_action_ts: Optional[datetime] = None
        self._daily_trades: int = 0

    # ------------------------------------------------------------------
    # Process signal → execution plan
    # ------------------------------------------------------------------

    def process_signal(
        self,
        signal: ORBSignal,
        now_et: datetime,
        equity: float,
        floating_pnl: float,
        daily_pnl: float,
        overall_pnl: float,
    ) -> Optional[Dict[str, Any]]:
        """Gate a signal through all checks.  Returns execution plan or None.

        Returns
        -------
        dict with keys:
            symbol, bias, shares, entry, stop, tp, delay, actions
        or None if signal is rejected.
        """
        self.risk.equity = equity
        self.risk._floating_pnl = floating_pnl
        self.risk._daily_closed_pnl = daily_pnl - floating_pnl

        # Gate 1: Session check
        if not self.session.can_trade_now(now_et):
            logger.debug("SKIP %s: session not active / day skip / cap hit", signal.symbol)
            return None

        # Gate 2: Session end buffer (no new entries after entry_end minus buffer)
        from challenge.stealth.session_simulator import SessionSimulator as SS
        entry_end_min = SS._time_to_minutes(SS._parse_hm(self.session.orb_entry_end))
        now_min = now_et.hour * 60 + now_et.minute
        if now_min >= entry_end_min - 5:  # 5-min buffer before end
            logger.debug("SKIP %s: near session end", signal.symbol)
            return None

        # Gate 3: Daily floating loss check (-$30)
        if self.risk.is_daily_loss_limit_hit():
            logger.warning("SKIP %s: daily loss limit hit (%.2f)", signal.symbol, daily_pnl)
            return None

        # Gate 4: Overall floating loss check (-$90)
        if self.risk.is_overall_loss_buffer_hit():
            logger.warning("SKIP %s: overall loss buffer hit (%.2f)", signal.symbol, overall_pnl)
            return None

        # Gate 5: Earnings skip
        if self.timer.is_earnings_day(signal.symbol, now_et.date()):
            logger.info("SKIP %s: earnings day", signal.symbol)
            return None

        # Gate 6: Min gap since last action
        if not self.timer.is_min_gap_ok(self._last_action_ts, now_et):
            logger.debug("SKIP %s: min gap not met", signal.symbol)
            return None

        # Gate 7: Humanized delay
        delay = self.timer.compute_delay(now_et)

        # Gate 8: Risk params (position sizing + SL:TP profiles)
        sl_m, tp_m = self.risk.select_sl_tp_profile()
        stop_dist = abs(signal.entry - signal.stop) * sl_m
        tp_dist = abs(signal.entry - signal.stop) * tp_m

        if signal.bias == "long":
            stop = signal.entry - stop_dist
            tp = signal.entry + tp_dist
        else:
            stop = signal.entry + stop_dist
            tp = signal.entry - tp_dist

        shares = self.risk.position_size(stop_dist, signal.entry)
        if shares < 1:
            logger.info("SKIP %s: sizing < 1 share", signal.symbol)
            return None

        # Verify notional
        if not self.risk.notional_ok(shares, signal.entry):
            logger.info("SKIP %s: notional exceeds buying power", signal.symbol)
            return None

        # Build execution plan
        # Determine action method: 70% DOM click, 30% hotkey
        use_hotkey = self.humanizer.use_hotkey()
        if use_hotkey:
            key = self.humanizer.execute_hotkey(
                "buy_market" if signal.bias == "long" else "sell_market"
            )
            method = {"type": "hotkey", "key": key}
        else:
            method = {"type": "dom_click", "selector": f"[data-action='{signal.bias}']"}

        plan = {
            "symbol": signal.symbol,
            "bias": signal.bias,
            "shares": shares,
            "entry": round(signal.entry, 2),
            "stop": round(stop, 2),
            "tp": round(tp, 2),
            "delay": round(delay, 2),
            "method": method,
            "pre_trade_activity": self.humanizer.pre_trade_activity(),
            "post_trade_activity": [],  # filled after execution
            "profile_sl_m": sl_m,
            "profile_tp_m": tp_m,
            "volume_ratio": signal.volume_ratio,
            "range_pct": signal.range_pct,
        }

        logger.info(
            "EXEC PLAN: %s %s x%d @ %.2f (SL %.2f / TP %.2f) delay %.1fs via %s",
            signal.bias.upper(), signal.symbol, shares,
            signal.entry, stop, tp, delay, method["type"],
        )

        return plan

    # ------------------------------------------------------------------
    # Manage open positions
    # ------------------------------------------------------------------

    def manage_position(
        self,
        position: Dict[str, Any],
        now_et: datetime,
        floating_pnl: float,
    ) -> List[Dict[str, Any]]:
        """Evaluate an open position and return action list.

        Parameters
        ----------
        position : dict
            Keys: symbol, side, qty, entry, stop, tp, remaining_shares,
            already_partialed, ...
        now_et : datetime
        floating_pnl : float
        """
        actions: List[Dict[str, Any]] = []
        side = position.get("side", position.get("bias", ""))
        entry = position.get("entry", 0)
        current_sl = position.get("stop", 0)
        tp_price = position.get("tp", 0)
        total_shares = position.get("qty", position.get("shares", 0))
        remaining = position.get("remaining_shares", total_shares)
        already_partialed = position.get("already_partialed", False)
        current_price = position.get("current_price", entry)

        # Use equity humanizer
        eq_actions = self.equity_humanizer.evaluate_position(
            side=side,
            entry_price=entry,
            current_price=current_price,
            current_sl=current_sl,
            tp_price=tp_price,
            total_shares=total_shares,
            remaining_shares=remaining,
            already_partialed=already_partialed,
        )

        for eq_action in eq_actions:
            if eq_action["action"] == "trailing_sl":
                # Trailing SL → modify via browser
                delay = self.timer.compute_delay(now_et)
                actions.append({
                    "action": "modify_stop",
                    "symbol": position.get("symbol", ""),
                    "new_stop": eq_action["new_sl"],
                    "delay": delay,
                    "method": {"type": "dom_click", "selector": "[data-action='modify']"},
                })
            elif eq_action["action"] == "partial_exit":
                delay = self.timer.compute_delay(now_et)
                actions.append({
                    "action": "partial_close",
                    "symbol": position.get("symbol", ""),
                    "shares": eq_action["shares"],
                    "delay": delay,
                    "method": {"type": "dom_click", "selector": "[data-action='close']"},
                })
            elif eq_action["action"] == "early_close":
                delay = self.timer.close_delay()
                actions.append({
                    "action": "close_position",
                    "symbol": position.get("symbol", ""),
                    "shares": remaining,
                    "delay": delay,
                    "method": {"type": "dom_click", "selector": "[data-action='close']"},
                })

        return actions

    # ------------------------------------------------------------------
    # Continuous equity monitor
    # ------------------------------------------------------------------

    def should_force_close(self, floating_pnl: float, overall_pnl: float) -> bool:
        """Check if positions should be force-closed immediately."""
        self.risk._floating_pnl = floating_pnl
        self.risk._equity = self.risk.start_balance + overall_pnl
        return self.risk.should_force_close()

    def force_close_plan(self, position: Dict[str, Any], now_et: datetime) -> Dict[str, Any]:
        """Build a forced close execution plan."""
        delay = self.timer.close_delay()
        return {
            "action": "close_position",
            "symbol": position.get("symbol", ""),
            "shares": position.get("remaining_shares", position.get("qty", 0)),
            "delay": delay,
            "method": {"type": "dom_click", "selector": "[data-action='close']"},
            "forced": True,
            "reason": "daily_or_overall_loss_limit",
        }

    # ------------------------------------------------------------------
    # Action completion
    # ------------------------------------------------------------------

    def record_action(self) -> None:
        """Record that a browser action was completed."""
        self._last_action_ts = datetime.now()
        self.timer.record_action()
        self.humanizer.record_action()
        self._daily_trades += 1

    def new_day(self) -> None:
        """Reset daily counters."""
        self._daily_trades = 0
        self.timer.reset_orders_today()

    @property
    def daily_trades(self) -> int:
        return self._daily_trades
