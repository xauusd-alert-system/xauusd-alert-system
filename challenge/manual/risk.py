# -*- coding: utf-8 -*-
"""Risk profiles and daily state machine — prop-challenge manual system (ТЗ §2, §3).

Implements the three risk profiles (C / B / A), per-day limits, stop-day rules,
profit lock, challenge pause and drawdown-based risk scaling. Profile switching
is allowed ONLY between days, never intraday.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass, field

STAGE = {
    1: {"target_usd": 80.0, "target_pct": 0.08,
        "max_daily_loss_usd": 50.0, "max_daily_loss_pct": 0.05,
        "max_dd_usd": 100.0, "max_dd_pct": 0.10,
        # RESEARCH (deep-research-report): experts recommend NOT using profit
        # lock — it caps upside without reducing risk. Remove from active rules.
        # Kept as 0 so the evaluate() code path still works if needed.
        "profit_lock_usd": 0.0, "profit_lock_pct": 0.0,
        "pause_usd": 60.0, "pause_pct": 0.06},
    2: {"target_usd": 60.0, "target_pct": 0.06,
        "max_daily_loss_usd": 50.0, "max_daily_loss_pct": 0.05,
        "max_dd_usd": 80.0, "max_dd_pct": 0.08,
        "profit_lock_usd": 0.0, "profit_lock_pct": 0.0,
        "pause_usd": 50.0, "pause_pct": 0.05},
}

# ТЗ §3: base parameters per profile. pause_usd is per stage.
# only_a: False everywhere (2026-08-21). 24w data: the A/B grade (retrace
# depth) does NOT predict outcomes — A avgR -0.073 vs B +0.029, and the split
# inverts with regime (Feb-Apr prefers shallow, Jun-Aug prefers deep). Gating
# on grade cuts the worse-or-equal half and removes trades; the real protection
# is size + trade count, which are already scaled here.
#
# RESEARCH 2026-08-22: deep-research-report + us_stocks audit.
# - Risk 0.25-0.5% of $1000 = $2.5-$5 per trade (matches B/A).
# - Personal daily stop: experts recommend 3.5-4% ($35-40), well below
#   firm's -$50. Raised B daily_limit $15→$25 to avoid cutting winners.
# - Profit lock removed (experts: it limits upside unnecessarily).
# - 2-loss stop-day is validated by research (prevents tilt).
# - Commission feasibility: at $2.50 risk, min fee $2 leaves only $0.50
#   for stop movement. Added commission-aware sizing check.
PROFILES = {
    "C": {"risk_usd": 2.0, "risk_pct": 0.002,
          "daily_limit_usd": 10.0, "daily_limit_pct": 0.01,
          "max_trades": 2, "stop_after_losses": 1,
          "pause_usd": {1: 50.0, 2: 40.0}, "pause_pct": {1: 0.05, 2: 0.04},
          "only_a": False, "max_risk_usd": 2.0},
    "B": {"risk_usd": 2.5, "risk_pct": 0.0025,
          # RAISED from $15: research says profit lock caps upside.
          # Personal stop at -$25 is already 50% of firm's -$50.
          "daily_limit_usd": 25.0, "daily_limit_pct": 0.025,
          "max_trades": 3, "stop_after_losses": 2,
          "pause_usd": {1: 60.0, 2: 50.0}, "pause_pct": {1: 0.06, 2: 0.05},
          "only_a": False, "max_risk_usd": 2.5},
    "A": {"risk_usd": 5.0, "risk_pct": 0.005,
          # RAISED from $20: at 0.5% risk, $5 risk is at the upper edge
          # of the research-recommended range. Allow bigger daily swings.
          "daily_limit_usd": 30.0, "daily_limit_pct": 0.03,
          "max_trades": 3, "stop_after_losses": 2,
          "pause_usd": {1: 50.0, 2: 50.0}, "pause_pct": {1: 0.05, 2: 0.05},
          "only_a": False, "max_risk_usd": 5.0},
}

# ТЗ §2.3: drawdown scaling steps (applied between days).
# (total_drawdown_pct, risk_usd, max_trades, only_a)
DRAWDOWN_STEPS = [
    (0.030, 2.0, 2, False),   # -3%: risk 0.2%, 2 trades (both grades — grade
                              #       does not predict outcomes, see PROFILES)
    (0.045, 1.5, 1, False),   # -4.5% (S1) / -4% (S2): risk 0.15%, 1 trade
]

DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "manual", "day_state.json")


@dataclass
class DayState:
    stage: int = 1
    profile: str = "B"
    date: str = ""                      # local session date (YYYY-MM-DD)
    day_start_equity: float = 1000.0    # equity at start of day (balance)
    total_start_equity: float = 1000.0  # stage-start reference
    current_equity: float = 1000.0      # equity incl. floating P&L
    trades_today: int = 0
    losses_today: int = 0
    status: str = "active"              # active | stop_day | profit_locked | paused | no_trade
    status_reason: str = ""
    effective_risk_usd: float = 2.5     # after profile + drawdown scaling
    effective_max_trades: int = 3
    effective_stop_after_losses: int = 2   # audit B: per-day effective rule
    effective_only_a: bool = True
    clusters_used_today: list = field(default_factory=list)  # anti-correlation cap
    risk_reduced: bool = False
    paused_until: str = ""              # ISO date when a pause ends

    def daily_pnl(self) -> float:
        return self.current_equity - self.day_start_equity

    def total_pnl(self) -> float:
        return self.current_equity - self.total_start_equity

    def as_dict(self) -> dict:
        return asdict(self)


def _stage_pause_usd(stage: int, profile: str) -> float:
    return float(PROFILES[profile]["pause_usd"].get(stage, 60.0))


def effective_profile(stage: int, profile: str, total_pnl: float,
                      day_start_equity: float) -> dict:
    """ТЗ §2.3: drawdown-based scaling of the base profile. Returns the params
    that actually apply for the day (profile switching happens between days)."""
    base = dict(PROFILES[profile])
    total_pct = total_pnl / day_start_equity if day_start_equity else 0.0
    dd = -total_pct if total_pct < 0 else 0.0
    for step_dd, risk, max_trades, only_a in DRAWDOWN_STEPS:
        if dd >= step_dd:
            base["risk_usd"] = risk
            base["max_trades"] = max_trades
            base["only_a"] = only_a
    return base


class DailyStateMachine:
    """Encodes the hard rules of ТЗ §6.2 (violation = stop-day)."""

    def __init__(self, cfg=None, state_path: str = DEFAULT_STATE_PATH):
        self.state_path = state_path
        self.cfg = cfg or {}
        self.state = self._load() or DayState()

    def _load(self) -> DayState | None:
        if not os.path.exists(self.state_path):
            return None
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            return DayState(**{k: v for k, v in data.items()
                               if k in DayState.__dataclass_fields__})
        except Exception:
            return None

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state.as_dict(), f, indent=2, ensure_ascii=False)

    # ---- day lifecycle ----
    def start_day(self, stage: int, profile: str, day_start_equity: float,
                  total_start_equity: float, now: dt.datetime) -> dict:
        """Begin a new session day. Computes the effective profile params
        (base + drawdown scaling) and resets daily counters."""
        if self._is_paused(now):
            return {"ok": False, "reason": "paused",
                    "paused_until": self.state.paused_until}
        total_pnl = day_start_equity - total_start_equity
        eff = effective_profile(stage, profile, total_pnl, total_start_equity)
        self.state = DayState(
            stage=stage, profile=profile,
            date=now.date().isoformat(),
            day_start_equity=day_start_equity,
            total_start_equity=total_start_equity,
            current_equity=day_start_equity,
            effective_risk_usd=float(eff["risk_usd"]),
            effective_max_trades=int(eff["max_trades"]),
            effective_stop_after_losses=int(eff["stop_after_losses"]),
            effective_only_a=bool(eff["only_a"]),
            risk_reduced=(float(eff["risk_usd"]) < float(PROFILES[profile]["risk_usd"])),
        )
        self.save()
        return {"ok": True, "effective": eff,
                "day_start_equity": day_start_equity,
                "total_pnl": total_pnl,
                "risk_reduced": self.state.risk_reduced}

    def _is_paused(self, now: dt.datetime) -> bool:
        if not self.state.paused_until:
            return False
        try:
            end = dt.date.fromisoformat(self.state.paused_until)
        except ValueError:
            return False
        return now.date() < end

    def record_trade(self, result_usd: float, was_planned: bool = True,
                     violation: str = "", cluster: str = "") -> None:
        """Register a closed trade for the day. Hard-rule violations force a
        stop-day immediately (ТЗ §6.2)."""
        self.state.trades_today += 1
        if result_usd < 0:
            self.state.losses_today += 1
        if cluster and cluster not in (self.state.clusters_used_today or []):
            self.state.clusters_used_today.append(cluster)
        if violation:
            self.state.status = "stop_day"
            self.state.status_reason = "violation: " + violation
            self.save()
            return
        self._evaluate_day()
        self.save()

    def update_equity(self, equity: float) -> str:
        """Feed current equity (incl. floating P&L). Returns the action the
        trader must take: 'trade' | 'stop_day' | 'flatten_day' | 'halt'."""
        self.state.current_equity = equity
        action = self._evaluate_day()
        self.save()
        return action

    def _evaluate_day(self) -> str:
        s = self.state
        st = STAGE.get(s.stage, STAGE[1])
        daily = s.current_equity - s.day_start_equity
        total = s.current_equity - s.total_start_equity
        pause_usd = _stage_pause_usd(s.stage, s.profile)
        eff = effective_profile(s.stage, s.profile, total, s.total_start_equity)

        if total <= -pause_usd:
            # Audit A 2026-08-23: anchor the pause ONCE at the transition.
            # Previously every equity update while paused re-wrote
            # paused_until = today+5d, so monitoring during a pause slid the
            # end date forward forever.
            if s.status != "paused":
                s.status = "paused"
                s.status_reason = f"challenge pause -{pause_usd:.0f}$ reached ({total:+.2f})"
                s.paused_until = (dt.date.today() + dt.timedelta(days=5)).isoformat()
            return "halt"
        if daily <= -eff["daily_limit_usd"]:
            s.status = "stop_day"
            s.status_reason = f"daily limit ({daily:+.2f} <= -{eff['daily_limit_usd']:.0f}$)"
            return "flatten_day"
        if st["profit_lock_usd"] > 0 and daily >= st["profit_lock_usd"]:
            s.status = "profit_locked"
            s.status_reason = f"profit lock ({daily:+.2f} >= +{st['profit_lock_usd']:.0f}$)"
            return "flatten_day"
        if s.losses_today >= eff["stop_after_losses"]:
            s.status = "stop_day"
            s.status_reason = f"{s.losses_today} losses today (stop-day rule)"
            return "flatten_day"
        if s.trades_today >= eff["max_trades"]:
            s.status = "stop_day"
            s.status_reason = f"max {eff['max_trades']} trades/day reached"
            return "flatten_day"
        s.status = "active"
        s.status_reason = "ok"
        return "trade"

    def can_trade(self, setup_class: str = "A", cluster: str = "") -> tuple[bool, str]:
        """ТЗ §5.2 / §6: gate a signal before entry."""
        s = self.state
        if s.status != "active":
            return False, f"day status is {s.status} ({s.status_reason})"
        if s.trades_today >= s.effective_max_trades:
            return False, f"max {s.effective_max_trades} trades/day reached"
        # Audit B: use the day's effective rule instead of a hard-coded 2
        # (profile C stops after 1 loss; drawdown modes may differ).
        if s.losses_today >= s.effective_stop_after_losses:
            return False, (f"{s.losses_today} losses today "
                           f"(stop-day at {s.effective_stop_after_losses})")
        if s.effective_only_a and setup_class != "A":
            return False, f"profile {s.profile} allows A-setups only"
        # Anti-correlation cap (2026-08-23): max ONE position per cluster per
        # day — 12/18 watchlist names are bitcoin-beta and fire together.
        used = getattr(s, "clusters_used_today", []) or []
        if cluster and cluster in used:
            return False, (f"кластер «{cluster}» уже торговался сегодня "
                           f"(анти-корреляционный кап: 1 позиция на кластер)")
        return True, "ok"

    def position_size(self, price: float, stop_price: float, bias: str,
                      commission_per_share: float = 0.0) -> int:
        """Fractional share count so the max loss on a full stop-out equals
        the effective risk in dollars (ТЗ §4.6).

        RESEARCH 2026-08-22: commission-aware sizing. At $2.50 risk and
        $1/share commission, two-side commission eats $2, leaving only $0.50
        for the actual stop. This method now subtracts estimated round-trip
        commission from the risk budget before computing qty.
        """
        s = self.state
        stop_dist = abs(price - stop_price)
        if stop_dist <= 0 or price <= 0:
            return 0
        # Subtract estimated round-trip commission from risk budget
        risk_after_costs = s.effective_risk_usd - 2 * commission_per_share
        if risk_after_costs <= 0:
            return 0  # commissions exceed risk budget — no-trade
        qty = risk_after_costs / stop_dist
        return round(qty, 2)

    def risk_check(self, price: float, stop_price: float) -> dict:
        """ТЗ §5.2: reject if the computed stop risk exceeds the profile cap."""
        s = self.state
        stop_dist = abs(price - stop_price)
        qty = s.effective_risk_usd / stop_dist if stop_dist else 0.0
        risk_usd = qty * stop_dist
        cap = float(PROFILES[s.profile]["max_risk_usd"])
        return {"risk_usd": round(risk_usd, 2),
                "qty": round(qty, 2),
                "cap_usd": cap,
                "ok": risk_usd <= cap + 1e-9}


def profile_params(stage: int, profile: str, total_pnl: float,
                   reference_equity: float) -> dict:
    eff = effective_profile(stage, profile, total_pnl, reference_equity)
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
