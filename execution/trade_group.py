"""
TradeGroupSpec v1 — единый domain-контракт жизненного цикла сделки.

ТЗ «TradeGroupSpec v1» (2026-08-16): система должна иметь ОДИН источник истины
для геометрии сделки. ML отвечает за side/confidence/regime/metadata; детерминированный
geometry/risk engine — за entry/step/TP1/TP2/TP3/SL/risk/volume/allocation.

Гарантии этого модуля:

* ``TradeGroupSpec`` — immutable (pydantic frozen): после создания и валидации
  ``entry.reference``, ``TP1/TP2/TP3/SL``, ``stepPrice``, ``profileId`` не меняются.
  Разрешён только lifecycle state change (``with_actual_fill`` фиксирует фактический
  fill, не трогая geometry).
* Идентификаторы разделены: ``signalId`` / ``intentId`` / ``groupId`` / ``legId``
  (+ broker ids на уровне исполнения).
* Риск считается ОДИН раз на группу: ``GroupRisk.max_cash``/``max_pct`` против
  ``estimated_loss_at_sl`` — никогда не 3 × риск по legs.
* State machine: DRAFT → VALIDATED → SUBMITTED → OPENED → TP1_FILLED →
  BE_REQUESTED → BE_CONFIRMED → TP2_FILLED → TP3_FILLED → RECONCILED;
  терминальные: STOPPED/REJECTED/EXPIRED/CANCELLED/FAILED.
* ``allocate_leg_volumes`` — детерминированный volume allocator:
  total один раз → leg1 floor → leg2 floor → leg3 = остаток (ТЗ §15).
"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

GROUP_SCHEMA_VERSION = "trade-group.v1"

Mode = Literal["research", "paper", "demo", "live"]
Side = Literal["long", "short"]

# P0-7: max allowed |actual_fill - entry.reference| / entry.reference.
# Overridable via execution.max_actual_fill_deviation in config.yaml.
DEFAULT_MAX_FILL_DEVIATION = 0.05


def get_max_fill_deviation() -> float:
    """Read the fill-deviation threshold from config; fall back to 5%."""
    try:
        from config.loader import load_config

        cfg = load_config() or {}
        value = ((cfg.get("execution") or {}).get("max_actual_fill_deviation"))
        if value is None:
            return DEFAULT_MAX_FILL_DEVIATION
        v = float(value)
        if not 0.0 < v <= 1.0:
            return DEFAULT_MAX_FILL_DEVIATION
        return v
    except Exception:
        return DEFAULT_MAX_FILL_DEVIATION


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------

class GroupState(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    OPENED = "OPENED"
    TP1_FILLED = "TP1_FILLED"
    BE_REQUESTED = "BE_REQUESTED"
    BE_RETRY = "BE_RETRY"
    BE_CONFIRMED = "BE_CONFIRMED"
    TP2_FILLED = "TP2_FILLED"
    TP3_FILLED = "TP3_FILLED"
    RECONCILED = "RECONCILED"
    STOPPED = "STOPPED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    # P1.5.1 partial-submission compensation states (ТЗ P1.5.1 §2/§8):
    # a partial open runs a controlled compensation flow BEFORE the group is
    # allowed to reach FAILED; FAILED_WITH_OPEN_RISK is explicitly NON-terminal
    # so reconciliation keeps trying until open risk is zero.
    PARTIAL_SUBMISSION = "PARTIAL_SUBMISSION"
    COMPENSATION_REQUESTED = "COMPENSATION_REQUESTED"
    COMPENSATION_CONFIRMED = "COMPENSATION_CONFIRMED"
    FAILED_WITH_OPEN_RISK = "FAILED_WITH_OPEN_RISK"


TERMINAL_STATES = frozenset({
    GroupState.RECONCILED, GroupState.STOPPED, GroupState.REJECTED,
    GroupState.EXPIRED, GroupState.CANCELLED, GroupState.FAILED,
})

GROUP_TRANSITIONS: dict[GroupState, frozenset[GroupState]] = {
    GroupState.DRAFT: frozenset({GroupState.VALIDATED, GroupState.REJECTED, GroupState.CANCELLED}),
    GroupState.VALIDATED: frozenset({GroupState.SUBMITTED, GroupState.REJECTED,
                                     GroupState.EXPIRED, GroupState.CANCELLED}),
    GroupState.SUBMITTED: frozenset({GroupState.OPENED, GroupState.REJECTED,
                                     GroupState.FAILED, GroupState.EXPIRED,
                                     GroupState.STOPPED,
                                     GroupState.PARTIAL_SUBMISSION}),
    GroupState.PARTIAL_SUBMISSION: frozenset({GroupState.COMPENSATION_REQUESTED,
                                              GroupState.FAILED_WITH_OPEN_RISK,
                                              GroupState.REJECTED}),
    GroupState.COMPENSATION_REQUESTED: frozenset({
        GroupState.COMPENSATION_CONFIRMED, GroupState.COMPENSATION_REQUESTED,
        GroupState.FAILED_WITH_OPEN_RISK}),
    GroupState.COMPENSATION_CONFIRMED: frozenset({GroupState.FAILED}),
    GroupState.FAILED_WITH_OPEN_RISK: frozenset({
        GroupState.COMPENSATION_REQUESTED, GroupState.COMPENSATION_CONFIRMED,
        GroupState.FAILED_WITH_OPEN_RISK}),
    GroupState.OPENED: frozenset({GroupState.TP1_FILLED, GroupState.STOPPED,
                                  GroupState.FAILED, GroupState.EXPIRED}),
    GroupState.TP1_FILLED: frozenset({GroupState.BE_REQUESTED, GroupState.STOPPED,
                                      GroupState.FAILED}),
    GroupState.BE_REQUESTED: frozenset({GroupState.BE_CONFIRMED, GroupState.BE_RETRY,
                                        GroupState.STOPPED, GroupState.FAILED}),
    GroupState.BE_RETRY: frozenset({GroupState.BE_CONFIRMED, GroupState.BE_RETRY,
                                    GroupState.FAILED, GroupState.STOPPED}),
    GroupState.BE_CONFIRMED: frozenset({GroupState.TP2_FILLED, GroupState.STOPPED,
                                        GroupState.FAILED}),
    GroupState.TP2_FILLED: frozenset({GroupState.TP3_FILLED, GroupState.STOPPED,
                                      GroupState.FAILED}),
    GroupState.TP3_FILLED: frozenset({GroupState.RECONCILED, GroupState.STOPPED,
                                      GroupState.FAILED}),
}

# ТЗ §12: allowed lifecycle state changes (BE path is mandatory after TP1).
BE_PATH = (GroupState.TP1_FILLED, GroupState.BE_REQUESTED, GroupState.BE_CONFIRMED)


def validate_transition(current: GroupState, next_state: GroupState) -> bool:
    if current in TERMINAL_STATES:
        return False
    return next_state in GROUP_TRANSITIONS.get(current, frozenset())


def require_transition(current: GroupState, next_state: GroupState) -> None:
    if not validate_transition(current, next_state):
        raise ValueError(
            f"invalid group transition {current.value} -> {next_state.value}"
        )


class TradeLegState(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"


class BeStatus(str, Enum):
    NONE = "NONE"
    TP1_FILLED = "TP1_FILLED"
    BE_REQUESTED = "BE_REQUESTED"
    BE_RETRY = "BE_RETRY"
    BE_CONFIRMED = "BE_CONFIRMED"


# --------------------------------------------------------------------------
# Nested value objects (all frozen)
# --------------------------------------------------------------------------

class EntrySpec(BaseModel):
    model_config = {"frozen": True}

    low: float
    high: float
    reference: float
    actual_fill: float | None = None

    @model_validator(mode="after")
    def validate_zone(self):
        if not (self.low <= self.reference <= self.high):
            raise ValueError("entry.reference must lie within [entry.low, entry.high]")
        return self


class Geometry(BaseModel):
    model_config = {"frozen": True}

    version: str
    unit: str = "price"
    step_price: float = Field(gt=0.0)
    tp1: float
    tp2: float
    tp3: float
    sl: float


class TargetLegSpec(BaseModel):
    model_config = {"frozen": True}

    leg: int = Field(ge=1, le=3)
    price: float
    allocation: float = Field(gt=0.0, le=1.0)


class BreakEvenPolicy(BaseModel):
    model_config = {"frozen": True}

    trigger: str = "tp1_filled"
    raw_price_policy: str = "actual_fill"
    protected_price_policy: str = "actual_fill_plus_cost_buffer"
    apply_to: list[int] = Field(default_factory=lambda: [2, 3])

    @model_validator(mode="after")
    def validate_trigger(self):
        if self.trigger != "tp1_filled":
            raise ValueError("breakEven.trigger must be 'tp1_filled' (ТЗ §16)")
        return self


class GroupRisk(BaseModel):
    model_config = {"frozen": True}

    currency: str = "USD"
    max_cash: float = Field(gt=0.0)
    max_pct: float = Field(gt=0.0, le=1.0)
    estimated_loss_at_sl: float = Field(ge=0.0)
    total_volume: float = Field(gt=0.0)


# --------------------------------------------------------------------------
# TradeGroupSpec
# --------------------------------------------------------------------------

class TradeGroupSpec(BaseModel):
    """Immutable TradeGroupSpec v1 (ТЗ §5)."""

    model_config = {"frozen": True}

    schema_version: str = GROUP_SCHEMA_VERSION
    group_id: str
    signal_id: str
    intent_id: str
    asset_key: str
    broker_symbol: str
    mode: Mode
    side: Side
    entry: EntrySpec
    geometry: Geometry
    targets: list[TargetLegSpec]
    break_even: BreakEvenPolicy
    risk: GroupRisk
    profile_id: str
    model_version: str
    model_hash: str
    config_hash: str
    strategy_version: str
    expires_at_utc_ms: int
    created_at_utc_ms: int
    # P1.6 §20–§22: lineage from source to approved spec.
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self):
        # ТЗ §5: required fields (pydantic already enforces presence; here we
        # additionally enforce non-empty identity and cross-field consistency).
        for field in ("group_id", "signal_id", "intent_id", "asset_key",
                      "broker_symbol", "profile_id", "model_version",
                      "model_hash", "config_hash", "strategy_version"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"{field} must not be empty")
        # Follow-up ТЗ §2: explicit direction-aware geometry chains. The
        # sign-based formula is replaced by readable per-direction ordering.
        if self.side == "long":
            if not (
                self.geometry.sl < self.entry.reference
                < self.geometry.tp1
                < self.geometry.tp2
                < self.geometry.tp3
            ):
                raise ValueError("invalid LONG geometry: expected "
                                 "SL < entry.reference < TP1 < TP2 < TP3")
        else:
            if not (
                self.geometry.tp3
                < self.geometry.tp2
                < self.geometry.tp1
                < self.entry.reference
                < self.geometry.sl
            ):
                raise ValueError("invalid SHORT geometry: expected "
                                 "TP3 < TP2 < TP1 < entry.reference < SL")
        total_alloc = sum(t.allocation for t in self.targets)
        if abs(total_alloc - 1.0) > 1e-6:
            raise ValueError(f"target allocations must sum to 1.0, got {total_alloc}")
        if len(self.targets) != 3 or {t.leg for t in self.targets} != {1, 2, 3}:
            raise ValueError("trade-group.v1 requires exactly three target legs 1/2/3")
        if self.expires_at_utc_ms <= 0:
            raise ValueError("expires_at_utc_ms must be positive (TTL)")
        return self

    def require_execution_provenance(self) -> None:
        """P1.6 §22/§23: an APPROVED (execution-bound) spec must carry the full
        lineage. The base model stays constructible (legacy tests / paper
        fixtures), but the execution path calls this before approval."""
        prov = self.provenance or {}
        required_ids = ("market_snapshot_id", "feature_snapshot_id",
                        "model_inference_id", "model_hash", "profile_id",
                        "broker_snapshot_id", "cost_snapshot_id",
                        "geometry_hash", "provenance_hash")
        missing = [key for key in required_ids if not prov.get(key)]
        if missing:
            raise ValueError(
                f"trade-group provenance incomplete: missing {missing}"
            )
        if prov.get("geometry_hash") != self.geometry_hash():
            raise ValueError(
                "provenance.geometry_hash must equal the spec geometry_hash"
            )
        if prov.get("provenance_hash") != self.provenance_hash():
            raise ValueError(
                "provenance.provenance_hash must equal the spec provenance_hash"
            )

    # --- identity / hashing -------------------------------------------------

    def geometry_hash(self) -> str:
        """Hash over the immutable geometry + risk; stable across actual_fill.

        P1.6 §21: ``geometry_hash`` = ЧТО получилось; ``provenance_hash`` =
        ИЗ ЧЕГО получилось (lineage of parent snapshot ids).

        P0-3: the FULL risk block (including ``estimated_loss_at_sl``) is
        hashed. Risk is part of the validated geometry contract — silently
        mutating it must invalidate the hash and fail
        ExecutionIntentMismatch, otherwise a group could trade with the
        original TP/SL ladder but different capital at risk.
        """
        payload = json.dumps({
            "group_id": self.group_id,
            "asset_key": self.asset_key,
            "side": self.side,
            "entry_reference": self.entry.reference,
            "geometry": self.geometry.model_dump(),
            "targets": [t.model_dump() for t in self.targets],
            "break_even": self.break_even.model_dump(),
            "risk": self.risk.model_dump(),
            "profile_id": self.profile_id,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def provenance_hash(self) -> str:
        """Lineage hash over the parent snapshot ids (§21/§24)."""
        prov = self.provenance or {}
        payload = json.dumps({
            key: prov.get(key) for key in (
                "market_snapshot_id", "feature_snapshot_id", "model_inference_id",
                "model_hash", "profile_id", "broker_snapshot_id",
                "cost_snapshot_id",
            )
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def canonical_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # --- allowed lifecycle state change -------------------------------------

    def with_actual_fill(self, fill_price: float) -> "TradeGroupSpec":
        """Attach the broker-confirmed actual fill. Geometry stays immutable.

        ТЗ §6: ``entry.actualFill`` differs from ``entry.reference`` and is the
        only price used for break-even.

        P0-7: a fill deviating from ``entry.reference`` by more than
        ``max_deviation`` (fraction of the reference price; default 5%) is
        rejected — such a drift means the executed geometry no longer matches
        the validated one and break-even/TP prices would be meaningless.
        The threshold may be overridden via
        ``execution.max_actual_fill_deviation`` in config.yaml.
        """
        fill = float(fill_price)
        if fill <= 0.0:
            raise ValueError("actual fill price must be positive")
        if self.entry.actual_fill is not None and abs(self.entry.actual_fill - fill) > 1e-12:
            raise ValueError("actual_fill is already set and cannot be overwritten")
        max_deviation = get_max_fill_deviation()
        deviation = abs(fill - self.entry.reference) / self.entry.reference
        if deviation > max_deviation:
            raise ValueError(
                f"actual fill {fill} deviates {deviation:.4%} from reference "
                f"{self.entry.reference}, exceeding the allowed "
                f"{max_deviation:.2%}; geometry invalid for execution"
            )
        return self.model_copy(update={"entry": self.entry.model_copy(update={"actual_fill": fill})})

    def leg_price(self, leg: int) -> float:
        if leg == 1:
            return self.geometry.tp1
        if leg == 2:
            return self.geometry.tp2
        if leg == 3:
            return self.geometry.tp3
        raise ValueError(f"unknown leg {leg}")

    def as_geometry_payload(self) -> dict[str, Any]:
        """The ONE authoritative geometry dict shared by Telegram, MT5 request
        building and ledger payloads (ТЗ §20 parity contract)."""
        return {
            "schema_version": self.schema_version,
            "group_id": self.group_id,
            "asset_key": self.asset_key,
            "broker_symbol": self.broker_symbol,
            "mode": self.mode,
            "side": self.side,
            "entry_reference": self.entry.reference,
            "entry_actual_fill": self.entry.actual_fill,
            "step_price": self.geometry.step_price,
            "tp1": self.geometry.tp1,
            "tp2": self.geometry.tp2,
            "tp3": self.geometry.tp3,
            "sl": self.geometry.sl,
            "profile_id": self.profile_id,
        }

    def leg_allocation(self, leg: int) -> float:
        for target in self.targets:
            if target.leg == leg:
                return target.allocation
        raise ValueError(f"unknown leg {leg}")


# --------------------------------------------------------------------------
# Volume allocation (ТЗ §15) and group risk (ТЗ §14)
# --------------------------------------------------------------------------

INSUFFICIENT_VOLUME_FOR_THREE_LEGS = "INSUFFICIENT_VOLUME_FOR_THREE_LEGS"


def floor_to_step(value: float, step: float) -> float:
    """P0-6: exact floor-to-step via ``Decimal(str(x))`` (ТЗ, reference impl).

    Float arithmetic ``(value / step)`` produces dust like
    ``0.03 * (1/3) / 0.01 == 0.9999999999999997`` which int-truncates to the
    WRONG lot count. Parsing both operands through ``Decimal(str(...))``
    reproduces the decimal intent: 0.03 / 0.01 == 3 exactly."""
    if step <= 0.0:
        return round(float(value), 8)
    d_val = Decimal(str(value))
    d_step = Decimal(str(step))
    lots = (d_val / d_step).to_integral_value(rounding=ROUND_DOWN)
    return float(lots * d_step)


#: P0-6: lot-space tolerance for ALLOCATION intent. The product
#: ``total * (1/3)`` lands ~1e-16 below the exact lot boundary
#: (0.03 * (1/3) == 0.009999999999999998); a 1e-9 lot epsilon preserves the
#: intended split without the gross float rounding of the legacy code.
#: The public ``floor_to_step`` itself stays epsilon-free (pure ROUND_DOWN).
_ALLOCATION_LOT_EPS = Decimal("1e-9")


def allocate_leg_volumes(
    total_volume: float,
    allocations: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3),
    volume_step: float = 0.01,
    volume_min: float = 0.01,
    allow_short_legs: bool = False,
) -> list[float]:
    """Deterministic group volume allocation (ТЗ §15).

    total рассчитан ОДИН раз; leg1/leg2 floor-ятся к volume_step; leg3 = остаток.
    Каждый положительный leg обязан быть >= volume_min (fillable). Если после
    broker constraints невозможно создать все три legs — ValueError с кодом
    ``INSUFFICIENT_VOLUME_FOR_THREE_LEGS`` (или ``allow_short_legs=True`` для
    netting fallback с явной записью в ledger).

    P0-6: all splitting arithmetic is Decimal(str(x)) based — no float
    division dust. P1-11: a negative leg3 remainder is rejected even when
    ``allow_short_legs=True`` — the flag permits SHORT legs (leg < volume_min),
    never NEGATIVE volume."""
    if total_volume <= 0.0:
        raise ValueError("total_volume must be positive")
    step = float(volume_step)
    minimum = float(volume_min)
    if step <= 0.0 or minimum <= 0.0:
        raise ValueError("volume_step and volume_min must be positive")

    d_total = Decimal(str(total_volume))
    d_step = Decimal(str(step))

    def _floor(value: float) -> float:
        d_val = Decimal(str(value))
        lots = (d_val / d_step + _ALLOCATION_LOT_EPS) \
            .to_integral_value(rounding=ROUND_DOWN)
        return float(lots * d_step)

    leg1 = _floor(total_volume * allocations[0])
    leg2 = _floor(total_volume * allocations[1])
    # remainder in exact decimal (carries any sub-step dust, as before)
    leg3 = float(d_total - Decimal(str(leg1)) - Decimal(str(leg2)))

    volumes = [leg1, leg2, leg3]
    # P1-11: negative leg3 is invalid in EVERY mode — allow_short_legs only
    # relaxes the volume_min fillability gate for netting virtual legs.
    if leg3 < -1e-9:
        raise ValueError(
            f"{INSUFFICIENT_VOLUME_FOR_THREE_LEGS}: remainder leg3={leg3} < 0"
        )
    if not allow_short_legs:
        for i, volume in enumerate(volumes, 1):
            if volume <= 0.0 or volume < minimum - 1e-9:
                raise ValueError(
                    f"{INSUFFICIENT_VOLUME_FOR_THREE_LEGS}: leg{i} volume "
                    f"{volume} is not fillable (volume_min={minimum}, "
                    f"volume_step={step})"
                )
    return volumes


RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"


def check_group_risk(
    estimated_loss_at_sl: float,
    max_cash: float,
    max_pct: float,
    balance: float,
) -> tuple[bool, str | None]:
    """Group risk is checked ONCE per group (ТЗ §14): loss at SL must not exceed
    the cap, neither in cash nor as a percentage of balance."""
    if estimated_loss_at_sl > max_cash + 1e-9:
        return False, RISK_LIMIT_EXCEEDED
    if balance > 0.0 and estimated_loss_at_sl > max_pct * balance + 1e-9:
        return False, RISK_LIMIT_EXCEEDED
    return True, None


def check_group_not_expired(expires_at_utc_ms: int, now_ms: int) -> bool:
    return now_ms <= expires_at_utc_ms


SIGNAL_EXPIRED = "SIGNAL_EXPIRED"


# --------------------------------------------------------------------------
# Ids (ТЗ §24)
# --------------------------------------------------------------------------

def _compact_ts(now_ms: int) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now_ms / 1000.0))


def new_group_id(now_ms: int | None = None) -> str:
    now = int(now_ms) if now_ms is not None else time.time_ns() // 1_000_000
    return f"TG-{_compact_ts(now)}"


def new_intent_id(now_ms: int | None = None) -> str:
    now = int(now_ms) if now_ms is not None else time.time_ns() // 1_000_000
    return f"INT-{_compact_ts(now)}"


def new_leg_id(group_id: str, leg: int) -> str:
    return f"{group_id}-L{leg}"
