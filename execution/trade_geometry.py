"""
Trade geometry engine — pure, deterministic, testable without MT5 (ТЗ §7).

ML отвечает только за side/confidence/regime/metadata. ЭТОТ модуль отвечает за
детерминированную геометрию: entry zone, reference, step, TP1/TP2/TP3, SL,
gross R, cost-aware admissibility, tick alignment, broker distance, group risk
и volume allocation.

Единицы (ТЗ §4):
* ``price_distance`` = abs(price_a - price_b) — стратегическая геометрия задаётся
  в price units;
* ``terminal_points`` = price_distance / SYMBOL_POINT — только broker diagnostics.

Запрещено (ТЗ §10): молча растягивать TP/SL до произвольных 20/50/100 units.
Невалидный уровень → ``GeometryRejected`` с reason code:
TP1_TOO_CLOSE_TO_COST / STOP_BELOW_BROKER_MIN_DISTANCE / INVALID_TICK_ALIGNMENT /
PROFILE_NOT_VALIDATED / SIGNAL_EXPIRED / RISK_LIMIT_EXCEEDED /
INSUFFICIENT_VOLUME_FOR_THREE_LEGS.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from execution.trade_group import (
    BreakEvenPolicy,
    EntrySpec,
    Geometry,
    GroupRisk,
    GroupState,
    Side,
    TargetLegSpec,
    TradeGroupSpec,
    allocate_leg_volumes,
    check_group_not_expired,
    check_group_risk,
    new_group_id,
    new_intent_id,
)

# --------------------------------------------------------------------------
# Reason codes (ТЗ §10)
# --------------------------------------------------------------------------

TP1_TOO_CLOSE_TO_COST = "TP1_TOO_CLOSE_TO_COST"
STOP_BELOW_BROKER_MIN_DISTANCE = "STOP_BELOW_BROKER_MIN_DISTANCE"
INVALID_TICK_ALIGNMENT = "INVALID_TICK_ALIGNMENT"
PROFILE_NOT_VALIDATED = "PROFILE_NOT_VALIDATED"
SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"
INSUFFICIENT_VOLUME_FOR_THREE_LEGS = "INSUFFICIENT_VOLUME_FOR_THREE_LEGS"


class GeometryRejected(Exception):
    """Geometry/cost/broker validation failed with an explicit reason code."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


# --------------------------------------------------------------------------
# Pure snapshots (no MT5 dependency)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BrokerSnapshot:
    """Broker/symbol constraints snapshot (pure values, fetched by adapter)."""

    symbol_point: float = 0.01
    tick_size: float = 0.01
    digits: int = 2
    trade_stops_level: int = 0          # in terminal points
    trade_freeze_level: int = 0         # in terminal points
    spread: float = 0.0                 # in price units (bid/ask diff)
    contract_size: float = 100.0        # units per lot (XAU: 100 oz, FX: 100000)
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    execution_mode: str = "unknown"     # request/instant/market/exchange
    account_margin_mode: str = "unknown"  # hedging | netting | exchange
    balance: float = 0.0

    def min_stop_distance_price(self) -> float:
        """Broker minimum stop distance in price units (stops/freeze + spread)."""
        points = max(self.trade_stops_level, self.trade_freeze_level)
        return points * self.symbol_point + self.spread


@dataclass(frozen=True)
class CostSnapshot:
    """Cost inputs in price units, resolved by the caller from broker data."""

    round_trip_cost_price: float = 0.0      # spread + expected slippage (round trip)
    safety_buffer_price: float = 0.0        # ТЗ §10 safety buffer
    expected_exit_slippage: float = 0.0     # for break-even protection
    commission_buffer: float = 0.0          # for break-even protection

    def spread_buffer_price(self, spread: float) -> float:
        return spread


@dataclass(frozen=True)
class GeometryOutput:
    """Validated geometry output."""

    step_price: float
    tp1: float
    tp2: float
    tp3: float
    sl: float
    gross_r: float
    estimated_loss_at_sl: float
    leg_volumes: list[float]


# --------------------------------------------------------------------------
# Profile registry (config.trade_profiles)
# --------------------------------------------------------------------------

def resolve_profile(cfg: dict, asset_key: str, profile_id: str | None = None) -> dict:
    """Resolve a versioned trade profile from config.trade_profiles.

    ``profile_id=None`` picks the profile whose ``asset`` matches ``asset_key``
    (exactly one such profile required). Unknown profile/asset -> ValueError.
    """
    profiles = (cfg or {}).get("trade_profiles", {}) or {}
    if profile_id is not None:
        if profile_id not in profiles:
            raise ValueError(f"unknown trade profile {profile_id!r}")
        profile = profiles[profile_id]
    else:
        matches = [p for p in profiles.values() if p.get("asset") == asset_key]
        if len(matches) != 1:
            raise ValueError(
                f"cannot auto-resolve profile for {asset_key}: "
                f"{len(matches)} matching profiles in config"
            )
        profile = matches[0]
    if profile.get("asset") != asset_key:
        raise ValueError(
            f"profile {profile.get('asset')!r} does not match asset {asset_key!r}"
        )
    return profile


def validate_profile_gate(profile: dict) -> None:
    """Validation gate: ``validated: true`` required (ТЗ §8/§9/§30).

    Новый BTC-профиль обязан иметь ``validated: false`` до прохождения
    frozen-data validation; gate блокирует его (paper-only по построению).
    """
    if not bool(profile.get("validated", False)):
        raise GeometryRejected(
            PROFILE_NOT_VALIDATED,
            f"profile {profile.get('asset')!r} is not validated; "
            f"blocked by validation gate (status={profile.get('validation_status', 'unknown')})",
        )


# --------------------------------------------------------------------------
# Step / alignment / R helpers
# --------------------------------------------------------------------------

def calculate_step(profile: dict, atr: float) -> float:
    """Step in price units from the profile.

    ``step.source = atr``: step = atr * atr_mult, clamped to
    [min_price_distance, max_price_distance]. ``step.source = fixed``: exact.
    """
    step_cfg = profile.get("step", {}) or {}
    source = step_cfg.get("source", "atr")
    if source == "fixed":
        step = float(step_cfg["price_distance"])
    elif source == "atr":
        mult = float(step_cfg.get("atr_mult", 1.0))
        step = float(atr) * mult
    else:
        raise ValueError(f"unsupported step source {source!r}")
    minimum = step_cfg.get("min_price_distance")
    maximum = step_cfg.get("max_price_distance")
    if minimum is not None:
        step = max(step, float(minimum))
    if maximum is not None:
        step = min(step, float(maximum))
    if step <= 0.0:
        raise GeometryRejected(TP1_TOO_CLOSE_TO_COST, f"non-positive step {step}")
    return step


def align_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest tick-grid value (deterministic)."""
    tick = float(tick_size)
    if tick <= 0.0:
        return float(price)
    return round(round(float(price) / tick) * tick, 10)


def is_tick_aligned(price: float, tick_size: float, eps: float = 1e-9) -> bool:
    if tick_size <= 0.0:
        return True
    quotient = float(price) / float(tick_size)
    return abs(quotient - round(quotient)) < eps


def terminal_points(price_distance: float, symbol_point: float) -> float:
    """Broker-diagnostic unit only (ТЗ §4): price_distance / SYMBOL_POINT."""
    if symbol_point <= 0.0:
        raise ValueError("symbol_point must be positive")
    return price_distance / symbol_point


def calculate_gross_r(
    side: Side,
    entry: float,
    sl: float,
    targets: list[TargetLegSpec],
) -> float:
    """Gross R of the group (ТЗ §11 example form: ~0.432R for 4.30/8.40/11.90
    against a 19.00 stop with equal thirds)."""
    risk = abs(entry - sl)
    if risk <= 0.0:
        return 0.0
    total = 0.0
    for target in targets:
        total += (abs(target.price - entry) / risk) * target.allocation
    return total


# --------------------------------------------------------------------------
# Geometry calculation
# --------------------------------------------------------------------------

def calculate_geometry(
    *,
    profile: dict,
    side: Side,
    reference_price: float,
    atr: float,
    broker: BrokerSnapshot,
    cost: CostSnapshot,
    entry_low: float | None = None,
    entry_high: float | None = None,
    balance: float | None = None,
) -> GeometryOutput:
    """Deterministic geometry + cost/broker/risk validation (ТЗ §7/§10).

    Raises ``GeometryRejected`` with an explicit reason code instead of
    silently stretching levels.
    """
    validate_profile_gate(profile)

    step = calculate_step(profile, atr)
    direction = 1.0 if side == "long" else -1.0

    targets_cfg = profile.get("targets", {}) or {}
    multipliers = targets_cfg.get("multipliers", {}) or {}
    tp1_mult = float(multipliers.get("tp1", 1.0))
    tp2_mult = float(multipliers.get("tp2", 2.0))
    tp3_mult = float(multipliers.get("tp3", 3.0))
    stop_cfg = profile.get("stop", {}) or {}
    stop_mult = float(stop_cfg.get("multiplier", 2.0))

    tp1 = align_to_tick(reference_price + direction * step * tp1_mult, broker.tick_size)
    tp2 = align_to_tick(reference_price + direction * step * tp2_mult, broker.tick_size)
    tp3 = align_to_tick(reference_price + direction * step * tp3_mult, broker.tick_size)
    sl = align_to_tick(reference_price - direction * step * stop_mult, broker.tick_size)

    # --- tick alignment (ТЗ §10) -------------------------------------------
    for name, price in (("tp1", tp1), ("tp2", tp2), ("tp3", tp3), ("sl", sl)):
        if not is_tick_aligned(price, broker.tick_size):
            raise GeometryRejected(
                INVALID_TICK_ALIGNMENT,
                f"{name}={price} not aligned to tick_size={broker.tick_size}",
            )

    # --- broker minimum stop distance (ТЗ §10) ------------------------------
    sl_distance = abs(sl - reference_price)
    min_distance = broker.min_stop_distance_price()
    if sl_distance < min_distance - 1e-9:
        raise GeometryRejected(
            STOP_BELOW_BROKER_MIN_DISTANCE,
            f"SL distance {sl_distance:.6g} < broker minimum {min_distance:.6g} "
            f"(stops={broker.trade_stops_level}, freeze={broker.trade_freeze_level}, "
            f"spread={broker.spread:.6g})",
        )

    # --- cost-aware admissibility (ТЗ §10) ----------------------------------
    tp1_distance = abs(tp1 - reference_price)
    required = cost.round_trip_cost_price + cost.safety_buffer_price
    if tp1_distance <= required + 1e-9:
        raise GeometryRejected(
            TP1_TOO_CLOSE_TO_COST,
            f"TP1 net distance {tp1_distance:.6g} <= round-trip cost + buffer "
            f"{required:.6g}",
        )

    # --- volume allocation (ТЗ §15) -----------------------------------------
    volume_cfg = profile.get("volume", {}) or {}
    total_volume = float(volume_cfg.get("total", 0.0))
    if total_volume <= 0.0:
        raise GeometryRejected(INSUFFICIENT_VOLUME_FOR_THREE_LEGS,
                               "profile.volume.total is not set")
    allocation_cfg = profile.get("allocation", {}) or {}
    allocations = (
        float(allocation_cfg.get("tp1", 1 / 3)),
        float(allocation_cfg.get("tp2", 1 / 3)),
        float(allocation_cfg.get("tp3", 1 / 3)),
    )
    try:
        leg_volumes = allocate_leg_volumes(
            total_volume, allocations,
            volume_step=broker.volume_step, volume_min=broker.volume_min,
        )
    except ValueError as exc:
        raise GeometryRejected(INSUFFICIENT_VOLUME_FOR_THREE_LEGS, str(exc)) from exc

    # --- group risk (ТЗ §14): ONE risk check for the whole group -------------
    estimated_loss = total_volume * abs(sl - reference_price) * broker.contract_size
    risk_cfg = profile.get("risk", {}) or {}
    max_cash = float(risk_cfg.get("max_cash", 0.0))
    max_pct = float(risk_cfg.get("max_pct", 0.0))
    if max_cash <= 0.0:
        raise GeometryRejected(RISK_LIMIT_EXCEEDED, "profile.risk.max_cash is not set")
    balance_value = float(balance) if balance is not None else broker.balance
    ok, reason = check_group_risk(estimated_loss, max_cash, max_pct, balance_value)
    if not ok:
        raise GeometryRejected(
            reason or RISK_LIMIT_EXCEEDED,
            f"estimated loss at SL {estimated_loss:.6g} exceeds group cap "
            f"(max_cash={max_cash}, max_pct={max_pct}, balance={balance_value:.6g})",
        )

    gross_r = calculate_gross_r(
        side, reference_price, sl,
        [TargetLegSpec(leg=1, price=tp1, allocation=allocations[0]),
         TargetLegSpec(leg=2, price=tp2, allocation=allocations[1]),
         TargetLegSpec(leg=3, price=tp3, allocation=allocations[2])],
    )
    return GeometryOutput(
        step_price=step, tp1=tp1, tp2=tp2, tp3=tp3, sl=sl,
        gross_r=gross_r, estimated_loss_at_sl=estimated_loss,
        leg_volumes=leg_volumes,
    )


# --------------------------------------------------------------------------
# Break-even (ТЗ §17): actual fill only, never signal reference
# --------------------------------------------------------------------------

def compute_break_even(
    *,
    side: Side,
    actual_fill: float,
    cost: CostSnapshot,
    broker: BrokerSnapshot,
) -> dict[str, float | str]:
    """BE calculation: raw vs protected, tick-aligned (ТЗ §17/§18).

    BUY:  raw = fill; protected = fill + spread_buffer + exit_slippage + commission
    SELL: зеркально (знак обратный).

    The protected level is rounded AWAY from the market (long: ceil on tick
    grid, short: floor) so it can never be easier to hit than the raw BE.
    """
    direction = 1.0 if side == "long" else -1.0
    raw = float(actual_fill)
    protection = (
        broker.spread
        + cost.expected_exit_slippage
        + cost.commission_buffer
    )
    tick = float(broker.tick_size)

    def _ceil(price: float) -> float:
        return round(math.ceil(price / tick) * tick, 12)

    def _floor(price: float) -> float:
        return round(math.floor(price / tick) * tick, 12)

    raw_aligned = _ceil(raw) if side == "long" else _floor(raw)
    protected = raw + direction * protection
    if side == "long":
        protected = max(protected, raw)
        protected_aligned = _ceil(protected)
    else:
        protected = min(protected, raw)
        protected_aligned = _floor(protected)
    return {
        "raw_price": raw_aligned,
        "protected_price": protected_aligned,
        "requested_price": None,
        "confirmed_price": None,
        "status": "NONE",
    }


# --------------------------------------------------------------------------
# Pipeline signal -> TradeGroupSpec bridge (ТЗ §27 realtime/pipeline.py intent)
# --------------------------------------------------------------------------

def build_trade_group_from_signal(
    signal: dict,
    *,
    cfg: dict,
    asset_key: str,
    profile_id: str | None,
    broker: BrokerSnapshot,
    cost: CostSnapshot,
    mode: str = "paper",
    now_ms: int | None = None,
    balance: float | None = None,
) -> TradeGroupSpec:
    """Build a validated TradeGroupSpec from an ML signal dict (paper/demo path).

    The ML signal provides side/confidence/regime/model metadata only; every
    price level comes from the deterministic profile engine. Raises
    ``GeometryRejected`` (with reason code) when the signal or profile cannot
    produce an admissible group.
    """
    now = int(now_ms) if now_ms is not None else time.time_ns() // 1_000_000
    bias = signal.get("bias")
    if bias not in {"long", "short"}:
        raise GeometryRejected("INVALID_SIGNAL_SIDE", f"bias={bias!r} cannot form a group")

    if profile_id is None:
        profiles = (cfg or {}).get("trade_profiles", {}) or {}
        matches = [key for key, p in profiles.items() if p.get("asset") == asset_key]
        if len(matches) != 1:
            raise ValueError(
                f"cannot auto-resolve profile for {asset_key}: "
                f"{len(matches)} matching profiles in config"
            )
        profile_id = matches[0]
    profile = resolve_profile(cfg, asset_key, profile_id)
    validate_profile_gate(profile)

    reference = float(signal.get("entry_reference") or signal.get("reference_price") or 0.0)
    if reference <= 0.0:
        zone = signal.get("entry_zone") or []
        if len(zone) == 2:
            reference = (float(zone[0]) + float(zone[1])) / 2.0
    if reference <= 0.0:
        raise GeometryRejected("INVALID_ENTRY", "no reference entry price in signal")

    expires_at = int(signal.get("expires_at_utc_ms")
                     or signal.get("expires_at_utc") or 0)
    if expires_at > 0 and not check_group_not_expired(expires_at, now):
        raise GeometryRejected(SIGNAL_EXPIRED,
                               f"signal expired at {expires_at}, now={now}")

    atr = float(signal.get("atr") or 0.0)
    if atr <= 0.0:
        raise GeometryRejected(TP1_TOO_CLOSE_TO_COST, "signal has no positive ATR")

    geometry = calculate_geometry(
        profile=profile, side=bias, reference_price=reference, atr=atr,
        broker=broker, cost=cost, balance=balance,
    )
    allocations = (
        float(profile.get("allocation", {}).get("tp1", 1 / 3)),
        float(profile.get("allocation", {}).get("tp2", 1 / 3)),
        float(profile.get("allocation", {}).get("tp3", 1 / 3)),
    )
    risk_cfg = profile.get("risk", {}) or {}
    break_even_cfg = profile.get("break_even", {}) or {}

    entry_low = float(signal["entry_zone"][0]) if signal.get("entry_zone") else reference
    entry_high = float(signal["entry_zone"][1]) if signal.get("entry_zone") else reference
    if entry_low > reference:
        entry_low = reference
    if entry_high < reference:
        entry_high = reference

    group_id = new_group_id(now)
    return TradeGroupSpec(
        group_id=group_id,
        signal_id=str(signal.get("signal_id") or f"legacy:{asset_key}:{now}"),
        intent_id=str(signal.get("intent_id") or new_intent_id(now)),
        asset_key=asset_key,
        broker_symbol=str(signal.get("broker_symbol") or asset_key),
        mode=mode,  # type: ignore[arg-type]
        side=bias,  # type: ignore[arg-type]
        entry=EntrySpec(low=entry_low, high=entry_high, reference=reference),
        geometry=Geometry(
            version=profile.get("geometry_version", f"{profile_id or asset_key}_v1"),
            unit=str(profile.get("unit", "price")),
            step_price=geometry.step_price,
            tp1=geometry.tp1, tp2=geometry.tp2, tp3=geometry.tp3, sl=geometry.sl,
        ),
        targets=[
            TargetLegSpec(leg=1, price=geometry.tp1, allocation=allocations[0]),
            TargetLegSpec(leg=2, price=geometry.tp2, allocation=allocations[1]),
            TargetLegSpec(leg=3, price=geometry.tp3, allocation=allocations[2]),
        ],
        break_even=BreakEvenPolicy(
            trigger=break_even_cfg.get("trigger", "tp1_filled"),
            raw_price_policy=break_even_cfg.get("raw_price_policy", "actual_fill"),
            protected_price_policy=break_even_cfg.get(
                "protected_price_policy", "actual_fill_plus_cost_buffer"),
            apply_to=list(break_even_cfg.get("apply_to", [2, 3])),
        ),
        risk=GroupRisk(
            currency=str(risk_cfg.get("currency", "USD")),
            max_cash=float(risk_cfg.get("max_cash", 0.0)),
            max_pct=float(risk_cfg.get("max_pct", 0.0)),
            estimated_loss_at_sl=geometry.estimated_loss_at_sl,
            total_volume=float(profile.get("volume", {}).get("total", 0.0)),
        ),
        profile_id=profile_id,
        model_version=str(signal.get("model_version") or "unknown"),
        model_hash=str(signal.get("model_hash") or signal.get("model_path") or "unknown"),
        config_hash=str(signal.get("config_hash") or "unknown"),
        strategy_version=str(signal.get("strategy_version") or "unknown"),
        expires_at_utc_ms=expires_at or (now + 24 * 3600 * 1000),
        created_at_utc_ms=now,
    )
