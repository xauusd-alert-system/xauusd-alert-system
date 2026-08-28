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
from dataclasses import dataclass

from execution.trade_group import (
    BreakEvenPolicy,
    EntrySpec,
    Geometry,
    GroupRisk,
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
    """Cost inputs in price units, resolved by the caller from broker data.

    P1.6 §17–§19: ``CostSnapshot()`` with zero costs must NEVER mean "cost = 0".
    The ``status`` field separates:

    * ``observed``    — spread from a real MT5 bid/ask snapshot + derived costs;
    * ``estimated``   — approved profile / empirical dataset values;
    * ``unavailable`` — no cost source; geometry must be REJECTED
      (``COST_DATA_UNAVAILABLE``).

    ``unavailable()`` is the explicit factory used instead of bare defaults.

    P1-8 / ТЗ 7.7: an explicit ``estimated`` snapshot requires at least one
    non-zero cost value (``estimated требует хотя бы одно ненулевое значение``).
    An all-zero ``estimated`` claim is internally contradictory — it silently
    advertises a cost source while carrying no costs — so it is rejected with
    :class:`ValueError`. ``unavailable`` with all-zero costs remains valid
    (that is exactly what "no data" means).
    """

    round_trip_cost_price: float = 0.0      # spread + expected slippage (round trip)
    safety_buffer_price: float = 0.0        # ТЗ §10 safety buffer
    expected_exit_slippage: float = 0.0     # for break-even protection
    commission_buffer: float = 0.0          # for break-even protection
    status: str | None = None               # observed | estimated | unavailable
    source: str = "unknown"
    source_id: str | None = None
    as_of_utc_ms: int | None = None

    def _all_costs_zero(self) -> bool:
        """True when every cost component is exactly zero."""
        return (self.round_trip_cost_price == 0.0
                and self.expected_exit_slippage == 0.0
                and self.commission_buffer == 0.0)

    def __post_init__(self) -> None:
        # Backward compatibility: an explicit CostSnapshot(...) with cost
        # values is treated as "estimated" (approved profile values); a BARE
        # CostSnapshot() with all-zero defaults is "unavailable" — never a
        # silent zero (P1.6 §18).
        if self.status is None:
            object.__setattr__(
                self, "status",
                "estimated" if not self._all_costs_zero() else "unavailable")
        if self.status not in {"observed", "estimated", "unavailable"}:
            raise ValueError(f"invalid CostSnapshot status {self.status!r}")
        if self.status == "estimated" and self._all_costs_zero():
            # P1-8 / ТЗ 7.7: "estimated" claims a cost source exists; all-zero
            # values contradict that claim (spec: estimated требует хотя бы
            # одно ненулевое значение). Reject loudly instead of silently
            # treating zero costs as real costs downstream.
            raise ValueError(
                "estimated CostSnapshot requires at least one non-zero cost "
                "(round_trip_cost_price, expected_exit_slippage or "
                "commission_buffer); use status='unavailable' when no cost "
                "data exists"
            )
        if self.status == "observed":
            if not self.source.strip() or self.source == "unknown":
                # observed costs MUST carry a real source; estimated without an
                # explicit source is allowed (approved profile values)
                raise ValueError(
                    "observed CostSnapshot requires a real source "
                    "(never 'unknown')"
                )

    @property
    def available(self) -> bool:
        return self.status in {"observed", "estimated"}

    def data_hash(self) -> str:
        import hashlib
        import json
        payload = json.dumps({
            "round_trip_cost_price": self.round_trip_cost_price,
            "safety_buffer_price": self.safety_buffer_price,
            "expected_exit_slippage": self.expected_exit_slippage,
            "commission_buffer": self.commission_buffer,
            "status": self.status, "source": self.source,
            "source_id": self.source_id, "as_of_utc_ms": self.as_of_utc_ms,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def spread_buffer_price(self, spread: float) -> float:
        return spread

    @staticmethod
    def unavailable() -> "CostSnapshot":
        """Explicit 'no cost source' — never a silent zero (P1.6 §18)."""
        return CostSnapshot(status="unavailable", source="unknown")

    @staticmethod
    def from_observed(
        *,
        spread: float,
        expected_slippage: float,
        commission: float,
        source_id: str,
        as_of_utc_ms: int,
        safety_buffer_price: float = 0.0,
        source: str = "mt5",
    ) -> "CostSnapshot":
        """Observed costs from a real MT5 bid/ask snapshot + approved extras."""
        return CostSnapshot(
            round_trip_cost_price=round(spread + expected_slippage + commission, 10),
            safety_buffer_price=safety_buffer_price,
            expected_exit_slippage=expected_slippage,
            commission_buffer=commission,
            status="observed",
            source=source,
            source_id=source_id,
            as_of_utc_ms=as_of_utc_ms,
        )


COST_DATA_UNAVAILABLE = "COST_DATA_UNAVAILABLE"


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

    # P1.6 §18: missing cost source BLOCKS approved geometry — a zero-cost
    # default must never be treated as "cost = 0".
    if not cost.available:
        raise GeometryRejected(
            COST_DATA_UNAVAILABLE,
            f"cost source unavailable (status={cost.status}); "
            f"geometry cannot be approved without observed/estimated costs",
        )

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

    # --- ATR sanity check (P0-2) ---------------------------------------------
    # atr_pct = atr / price. A tiny atr_pct means the step (built from ATR) will
    # not clear round-trip costs; a huge one means the quoted price or the ATR
    # is corrupt (wrong units, bad symbol feed). Both reject with an explicit
    # reason code instead of producing nonsense geometry. Bounds are per-profile
    # via trade_profiles.<id>.atr_sanity {min_atr_pct, max_atr_pct}.
    atr_cfg = profile.get("atr_sanity") or {}
    min_atr_pct = float(atr_cfg.get("min_atr_pct", 0.0005))
    max_atr_pct = float(atr_cfg.get("max_atr_pct", 0.03))
    atr_pct = float(atr) / float(reference_price)
    if atr_pct < min_atr_pct or atr_pct > max_atr_pct:
        raise GeometryRejected(
            TP1_TOO_CLOSE_TO_COST,
            f"ATR sanity check failed: atr={atr:.6g}, reference={reference_price:.6g} "
            f"-> atr_pct={atr_pct:.6%} outside allowed bounds "
            f"[{min_atr_pct:.4%}, {max_atr_pct:.2%}] "
            f"(trade_profiles.{profile.get('asset', '?')}.atr_sanity); "
            "step derived from this ATR would produce untradeable geometry",
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

# P0-1: fallback when a timeframe has no entry in execution.signal_ttl_ms.
# 2 hours, NOT the legacy hardcoded 24h (stale M1 signals must not trade
# half a day later).
DEFAULT_SIGNAL_TTL_MS = 2 * 3600 * 1000


def resolve_signal_ttl_ms(cfg: dict, asset_key: str) -> int:
    """Per-timeframe signal TTL from execution.signal_ttl_ms (P0-1).

    The timeframe is resolved through the single source of truth
    (config.resolve_asset_timeframe); missing/invalid config entries fall back
    to ``default`` (or DEFAULT_SIGNAL_TTL_MS if that is absent too).
    """
    try:
        from config.loader import resolve_asset_timeframe

        tf = resolve_asset_timeframe(cfg, asset_key)
    except Exception:
        tf = None

    ttl_cfg = (cfg or {}).get("execution", {}).get("signal_ttl_ms") or {}
    default_ttl = ttl_cfg.get("default", DEFAULT_SIGNAL_TTL_MS)
    try:
        return max(1, int(ttl_cfg.get(tf, default_ttl)))
    except (TypeError, ValueError):
        return max(1, int(default_ttl))


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
    spec = TradeGroupSpec(
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
        # P0-1: per-timeframe TTL instead of a hardcoded 24h (a signal without
        # an explicit expires_at on M1 lives 30 minutes, on M15 six hours etc.)
        expires_at_utc_ms=expires_at or (now + resolve_signal_ttl_ms(cfg, asset_key)),
        created_at_utc_ms=now,
        # P1.6 §20–§22: lineage ids required for an approved spec.
        provenance={
            "market_snapshot_id": str(signal.get("market_snapshot_id")
                                      or f"MARKET:{asset_key}:{int(reference * 1000)}"),
            "feature_snapshot_id": str(signal.get("feature_snapshot_id")
                                       or f"FEATURE:{asset_key}:{int(reference * 1000)}"),
            "model_inference_id": str(signal.get("model_inference_id")
                                      or f"INFERENCE:{asset_key}:{int(reference * 1000)}"),
            "model_hash": str(signal.get("model_hash")
                              or signal.get("model_path") or "unknown"),
            "profile_id": profile_id,
            "broker_snapshot_id": str(signal.get("broker_snapshot_id")
                                      or f"BROKER:{asset_key}:{int(reference * 1000)}"),
            "cost_snapshot_id": str(cost.source_id or f"COST:{asset_key}:{now}"),
            "geometry_hash": "PLACEHOLDER",
            "provenance_hash": "PLACEHOLDER",
        },
    )
    # Real hashes in a second pass: geometry_hash depends on the immutable
    # fields only, provenance_hash on the lineage ids (§21).
    prov = dict(spec.provenance)
    geometry_hash = spec.geometry_hash()
    prov["geometry_hash"] = geometry_hash
    spec = spec.model_copy(update={"provenance": prov})
    prov = dict(spec.provenance)
    prov["provenance_hash"] = spec.provenance_hash()
    return spec.model_copy(update={"provenance": prov})
