# -*- coding: utf-8 -*-
"""Analysis-only risk controls for the Hash Hedge US Stocks Headliners workflow.

This module never connects to a terminal and cannot place, modify, or close orders.
It evaluates a human-entered account snapshot, estimates share size and records the
manual trading-day state described in ``prop-challenge-system(1).md``.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from dataclasses import asdict, dataclass

ACCOUNT_SIZE_USD = 1000.0
STOCK_FEE_RATE = 0.0004
STOCK_MIN_FEE_USD = 1.0

STAGE = {
    1: {
        "target_usd": 80.0,
        "target_pct": 0.08,
        "max_daily_loss_usd": 50.0,
        "max_daily_loss_pct": 0.05,
        "max_dd_usd": 100.0,
        "max_dd_pct": 0.10,
        "profit_lock_usd": 20.0,
        "profit_lock_pct": 0.02,
        "pause_usd": 60.0,
        "pause_pct": 0.06,
    },
    2: {
        "target_usd": 60.0,
        "target_pct": 0.06,
        "max_daily_loss_usd": 50.0,
        "max_daily_loss_pct": 0.05,
        "max_dd_usd": 80.0,
        "max_dd_pct": 0.08,
        "profit_lock_usd": 15.0,
        "profit_lock_pct": 0.015,
        "pause_usd": 50.0,
        "pause_pct": 0.05,
    },
}

# Profile names follow the owner's updated specification. Profile A is never the
# default: selecting it requires an explicit human confirmation at day start.
PROFILES = {
    "C": {
        "risk_usd": 2.0,
        "max_risk_usd": 2.0,
        "risk_pct": 0.002,
        "daily_limit_usd": 10.0,
        "daily_limit_pct": 0.01,
        "max_trades": 2,
        "stop_after_losses": 1,
        "only_a": True,
        "allow_b_when_positive": False,
        "max_b_trades": 0,
        "pause_usd": {1: 50.0, 2: 40.0},
        "pause_pct": {1: 0.05, 2: 0.04},
        "requires_profit_buffer_usd": 0.0,
    },
    "B": {
        "risk_usd": 2.5,
        "max_risk_usd": 3.0,
        "risk_pct": 0.0025,
        "daily_limit_usd": 15.0,
        "daily_limit_pct": 0.015,
        "max_trades": 3,
        "stop_after_losses": 2,
        "only_a": True,
        "allow_b_when_positive": True,
        "max_b_trades": 1,
        "pause_usd": {1: 60.0, 2: 50.0},
        "pause_pct": {1: 0.06, 2: 0.05},
        "requires_profit_buffer_usd": 0.0,
    },
    "A": {
        "risk_usd": 3.0,
        "max_risk_usd": 5.0,
        "risk_pct": 0.003,
        "daily_limit_usd": 20.0,
        "daily_limit_pct": 0.02,
        "max_trades": 3,
        "stop_after_losses": 2,
        "only_a": True,
        "allow_b_when_positive": False,
        "max_b_trades": 0,
        "pause_usd": {1: 50.0, 2: 50.0},
        "pause_pct": {1: 0.05, 2: 0.05},
        "requires_profit_buffer_usd": 20.0,
    },
}

DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "manual",
    "day_state.json",
)


@dataclass
class DayState:
    stage: int = 1
    profile: str = "B"
    date: str = ""
    # Hash Hedge defines daily loss from start-of-day Balance, not Equity.
    day_start_balance: float = ACCOUNT_SIZE_USD
    total_start_balance: float = ACCOUNT_SIZE_USD
    current_equity: float = ACCOUNT_SIZE_USD
    trades_today: int = 0
    losses_today: int = 0
    b_trades_today: int = 0
    open_positions: int = 0
    status: str = "active"  # active | stop_day | profit_locked | paused | no_trade
    status_reason: str = ""
    effective_risk_usd: float = 2.5
    effective_max_trades: int = 3
    effective_only_a: bool = True
    risk_reduced: bool = False
    paused_until: str = ""

    def daily_pnl(self) -> float:
        return self.current_equity - self.day_start_balance

    def total_pnl(self) -> float:
        return self.current_equity - self.total_start_balance

    def as_dict(self) -> dict:
        return asdict(self)


def _stage_pause_usd(stage: int, profile: str) -> float:
    return float(PROFILES[profile]["pause_usd"].get(stage, 60.0))


def effective_profile(stage: int, profile: str, total_pnl: float,
                      reference_balance: float) -> dict:
    """Return the current profile after mandatory drawdown de-risking.

    At -3% the specification permits only A setups, $2 risk and two trades. At
    -4.5% in stage 1 or -4% in stage 2 it permits only A setups, $1.5 risk and
    one trade. The function never upgrades risk intraday.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    base = dict(PROFILES[profile])
    dd = max(0.0, -total_pnl / reference_balance) if reference_balance else 0.0
    first_limit = 0.045 if stage == 1 else 0.040
    if dd >= first_limit:
        base.update({"risk_usd": 1.5, "max_risk_usd": 1.5,
                     "max_trades": 1, "only_a": True,
                     "allow_b_when_positive": False, "max_b_trades": 0})
    elif dd >= 0.030:
        base.update({"risk_usd": 2.0, "max_risk_usd": 2.0,
                     "max_trades": 2, "only_a": True,
                     "allow_b_when_positive": False, "max_b_trades": 0})
    return base


def estimated_order_fee(notional: float, fee_rate: float = STOCK_FEE_RATE,
                        minimum_fee_usd: float = STOCK_MIN_FEE_USD) -> float:
    """Estimate one manually placed stock order's platform fee.

    The estimate is deliberately a gate input, not a statement of a future fill.
    The human must compare it with the terminal's displayed fee before entry.
    """
    return round(max(minimum_fee_usd, abs(notional) * fee_rate), 4)


def estimated_round_trip_fees(entry: float, stop: float, shares: int,
                              fee_rate: float = STOCK_FEE_RATE,
                              minimum_fee_usd: float = STOCK_MIN_FEE_USD) -> float:
    if shares <= 0:
        return 0.0
    return round(
        estimated_order_fee(entry * shares, fee_rate, minimum_fee_usd)
        + estimated_order_fee(stop * shares, fee_rate, minimum_fee_usd),
        4,
    )


def max_safe_shares(entry: float, stop: float, risk_budget_usd: float,
                    buying_power_available: float | None = None,
                    fee_rate: float = STOCK_FEE_RATE,
                    minimum_fee_usd: float = STOCK_MIN_FEE_USD) -> int:
    """Return the largest whole-share size whose planned stop loss plus fees fits.

    A zero result is a deliberate NO-GO: the minimum meaningful whole share is
    too expensive for the selected risk profile or buying-power cap.
    """
    stop_distance = abs(entry - stop)
    if entry <= 0 or stop_distance <= 0 or risk_budget_usd <= 0:
        return 0
    upper = int(risk_budget_usd // stop_distance)
    if buying_power_available is not None:
        upper = min(upper, int(max(0.0, buying_power_available) // entry))
    for shares in range(max(upper, 0), 0, -1):
        price_risk = shares * stop_distance
        fees = estimated_round_trip_fees(entry, stop, shares, fee_rate, minimum_fee_usd)
        if price_risk + fees <= risk_budget_usd + 1e-9:
            return shares
    return 0


class DailyStateMachine:
    """Local, analysis-only state machine for the user's hard and soft limits."""

    def __init__(self, cfg=None, state_path: str = DEFAULT_STATE_PATH):
        self.state_path = state_path
        self.cfg = cfg or {}
        self.state = self._load() or DayState()

    def _load(self) -> DayState | None:
        if not os.path.exists(self.state_path):
            return None
        try:
            with open(self.state_path, encoding="utf-8") as f:
                raw = json.load(f)
            # Safe migration from the old day_start_equity field.
            if "day_start_balance" not in raw and "day_start_equity" in raw:
                raw["day_start_balance"] = raw["day_start_equity"]
            if "total_start_balance" not in raw and "total_start_equity" in raw:
                raw["total_start_balance"] = raw["total_start_equity"]
            return DayState(**{k: v for k, v in raw.items()
                               if k in DayState.__dataclass_fields__})
        except Exception:
            return None

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state.as_dict(), f, indent=2, ensure_ascii=False)

    def start_day(self, stage: int, profile: str, day_start_balance: float,
                  total_start_balance: float, now: dt.datetime,
                  profile_a_confirmed: bool = False) -> dict:
        """Start a new manual session from values the user verified on dashboard."""
        if profile not in PROFILES:
            return {"ok": False, "reason": f"unknown profile {profile}"}
        if self._is_paused(now):
            return {"ok": False, "reason": "paused", "paused_until": self.state.paused_until}
        total_pnl = day_start_balance - total_start_balance
        base = PROFILES[profile]
        if profile == "A" and (not profile_a_confirmed or total_pnl < base["requires_profit_buffer_usd"]):
            return {"ok": False, "reason": "profile_A_requires_confirmed_20usd_stage_buffer"}
        eff = effective_profile(stage, profile, total_pnl, total_start_balance)
        self.state = DayState(
            stage=stage,
            profile=profile,
            date=now.date().isoformat(),
            day_start_balance=day_start_balance,
            total_start_balance=total_start_balance,
            current_equity=day_start_balance,
            effective_risk_usd=float(eff["risk_usd"]),
            effective_max_trades=int(eff["max_trades"]),
            effective_only_a=bool(eff["only_a"]),
            risk_reduced=float(eff["risk_usd"]) < float(PROFILES[profile]["risk_usd"]),
        )
        self.save()
        return {"ok": True, "effective": eff, "day_start_balance": day_start_balance,
                "total_pnl": total_pnl, "risk_reduced": self.state.risk_reduced}

    def _is_paused(self, now: dt.datetime) -> bool:
        if not self.state.paused_until:
            return False
        try:
            return now.date() < dt.date.fromisoformat(self.state.paused_until)
        except ValueError:
            return False

    def record_trade(self, result_usd: float, setup_class: str = "A",
                     was_planned: bool = True, violation: str = "") -> None:
        """Record a manually entered closed trade; this function sends no orders."""
        self.state.trades_today += 1
        if setup_class.upper() == "B":
            self.state.b_trades_today += 1
        if result_usd < 0:
            self.state.losses_today += 1
        if violation or not was_planned:
            self.state.status = "stop_day"
            self.state.status_reason = "violation: " + (violation or "trade not by plan")
        else:
            self._evaluate_day()
        self.save()

    def update_equity(self, equity: float, open_positions: int = 0) -> str:
        """Update a human-observed equity snapshot and return an advisory action."""
        self.state.current_equity = equity
        self.state.open_positions = max(0, int(open_positions))
        action = self._evaluate_day()
        self.save()
        return action

    def _evaluate_day(self) -> str:
        s = self.state
        st = STAGE.get(s.stage, STAGE[1])
        daily = s.daily_pnl()
        total = s.total_pnl()
        pause_usd = _stage_pause_usd(s.stage, s.profile)
        eff = effective_profile(s.stage, s.profile, total, s.total_start_balance)
        if total <= -pause_usd:
            s.status = "paused"
            s.status_reason = f"challenge pause {-pause_usd:.0f}$ reached ({total:+.2f})"
            s.paused_until = (dt.date.today() + dt.timedelta(days=2)).isoformat()
            return "halt"
        if daily <= -float(eff["daily_limit_usd"]):
            s.status = "stop_day"
            s.status_reason = f"personal daily limit ({daily:+.2f}$)"
            return "stop_day"
        if daily >= float(st["profit_lock_usd"]):
            s.status = "profit_locked"
            s.status_reason = f"profit lock ({daily:+.2f}$)"
            return "stop_day"
        if s.losses_today >= int(eff["stop_after_losses"]):
            s.status = "stop_day"
            s.status_reason = f"{s.losses_today} losses today"
            return "stop_day"
        if s.trades_today >= int(eff["max_trades"]):
            s.status = "stop_day"
            s.status_reason = f"max {eff['max_trades']} trades/day reached"
            return "stop_day"
        s.status = "active"
        s.status_reason = "ok"
        return "trade"

    def can_trade(self, setup_class: str = "A", minutes_to_close: float | None = None,
                  open_positions: int | None = None, news_red_zone: bool = False,
                  stop_defined: bool = True, target_defined: bool = True) -> tuple[bool, str]:
        """Return a manual-entry gate decision; a True result is never an order."""
        s = self.state
        eff = effective_profile(s.stage, s.profile, s.total_pnl(), s.total_start_balance)
        setup_class = setup_class.upper()
        if s.status != "active":
            return False, f"day status is {s.status} ({s.status_reason})"
        if (open_positions if open_positions is not None else s.open_positions) >= 1:
            return False, "one-open-position limit"
        if minutes_to_close is not None and minutes_to_close < 45:
            return False, "less than 45 minutes to session close"
        if news_red_zone:
            return False, "red-zone news"
        if not stop_defined or not target_defined:
            return False, "predefined stop and target required"
        if setup_class == "C":
            return False, "C setup is prohibited"
        if setup_class == "B":
            if not eff["allow_b_when_positive"]:
                return False, "profile permits A setups only"
            if s.daily_pnl() <= 0:
                return False, "B setup requires positive daily PnL"
            if s.b_trades_today >= int(eff["max_b_trades"]):
                return False, "one B setup per day limit"
        elif setup_class != "A":
            return False, "unknown setup class"
        return True, "manual review required"

    def position_size(self, price: float, stop_price: float, bias: str = "long",
                      buying_power_available: float | None = None) -> int:
        """Return a whole-share advisory size including estimated round-trip fees."""
        del bias  # Symmetric price-distance computation for long and short.
        return max_safe_shares(price, stop_price, self.state.effective_risk_usd,
                               buying_power_available)

    def risk_check(self, price: float, stop_price: float,
                   buying_power_available: float | None = None) -> dict:
        shares = self.position_size(price, stop_price, buying_power_available=buying_power_available)
        stop_distance = abs(price - stop_price)
        price_risk = round(shares * stop_distance, 4)
        fees = estimated_round_trip_fees(price, stop_price, shares)
        total = round(price_risk + fees, 4)
        cap = min(float(PROFILES[self.state.profile]["max_risk_usd"]),
                  self.state.effective_risk_usd)
        return {
            "shares": shares,
            "price_risk_usd": price_risk,
            "estimated_round_trip_fees_usd": fees,
            "planned_total_loss_usd": total,
            "cap_usd": cap,
            "notional_usd": round(shares * price, 4),
            "ok": shares >= 1 and total <= cap + 1e-9,
            "advisory": "manual terminal confirmation required",
        }


def profile_params(stage: int, profile: str, total_pnl: float,
                   reference_balance: float) -> dict:
    eff = effective_profile(stage, profile, total_pnl, reference_balance)
    st = STAGE.get(stage, STAGE[1])
    return {
        "profile": profile,
        "stage": stage,
        "risk_usd": eff["risk_usd"],
        "risk_pct": eff["risk_pct"],
        "daily_limit_usd": eff["daily_limit_usd"],
        "daily_limit_pct": eff["daily_limit_pct"],
        "max_trades": eff["max_trades"],
        "stop_after_losses": eff["stop_after_losses"],
        "pause_usd": _stage_pause_usd(stage, profile),
        "pause_pct": PROFILES[profile]["pause_pct"].get(stage),
        "profit_lock_usd": st["profit_lock_usd"],
        "profit_lock_pct": st["profit_lock_pct"],
        "only_a": eff["only_a"],
        "max_risk_usd": eff["max_risk_usd"],
    }
