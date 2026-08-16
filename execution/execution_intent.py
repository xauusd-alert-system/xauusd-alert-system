"""
ExecutionIntent — immutable validated execution intent (ТЗ P1.5 §6).

An ``ExecutionIntent`` is a frozen snapshot of what the MT5 executor is allowed
to send to the broker: it references ``TradeGroupSpec.geometry_hash()`` and the
executor MUST verify the hash is unchanged immediately before submission. If the
approved geometry drifted (impossible for a frozen spec, but enforced anyway),
submission is rejected instead of silently sending different levels.

``from_spec`` builds the intent from a validated spec; ``require_geometry_unchanged``
raises ``ExecutionIntentMismatch`` on any drift. The intent carries everything the
broker request builder needs: entry reference, the immutable TP ladder, SL, total
volume, per-leg volumes, risk envelope, profile id and TTL.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from execution.trade_group import GROUP_SCHEMA_VERSION, Side, TradeGroupSpec


class ExecutionIntentMismatch(RuntimeError):
    """The spec's geometry_hash differs from the intent's recorded hash."""


class ExecutionIntent(BaseModel):
    model_config = {"frozen": True}

    intent_id: str
    group_id: str
    schema_version: str = GROUP_SCHEMA_VERSION
    mode: str
    asset_key: str
    broker_symbol: str
    side: Side
    entry_reference: float
    tp1: float
    tp2: float
    tp3: float
    sl: float
    total_volume: float = Field(gt=0.0)
    leg_volumes: list[float] = Field(min_length=3, max_length=3)
    risk: dict[str, Any]
    profile_id: str
    expires_at_utc_ms: int
    geometry_hash: str
    created_at_utc_ms: int

    @classmethod
    def from_spec(cls, spec: TradeGroupSpec, intent_id: str | None = None) -> "ExecutionIntent":
        volumes = [
            round(spec.risk.total_volume * t.allocation, 8) for t in spec.targets
        ]
        return cls(
            intent_id=intent_id or spec.intent_id,
            group_id=spec.group_id,
            mode=spec.mode,
            asset_key=spec.asset_key,
            broker_symbol=spec.broker_symbol,
            side=spec.side,
            entry_reference=spec.entry.reference,
            tp1=spec.geometry.tp1,
            tp2=spec.geometry.tp2,
            tp3=spec.geometry.tp3,
            sl=spec.geometry.sl,
            total_volume=spec.risk.total_volume,
            leg_volumes=volumes,
            risk=spec.risk.model_dump(mode="json"),
            profile_id=spec.profile_id,
            expires_at_utc_ms=spec.expires_at_utc_ms,
            geometry_hash=spec.geometry_hash(),
            created_at_utc_ms=spec.created_at_utc_ms,
        )

    def verify_geometry(self, spec: TradeGroupSpec) -> bool:
        """True when the spec's geometry hash still matches this intent."""
        return self.geometry_hash == spec.geometry_hash()

    def require_geometry_unchanged(self, spec: TradeGroupSpec) -> None:
        if not self.verify_geometry(spec):
            raise ExecutionIntentMismatch(
                f"intent {self.intent_id} geometry_hash {self.geometry_hash} "
                f"no longer matches spec {spec.group_id} "
                f"({spec.geometry_hash()}); submission rejected"
            )
