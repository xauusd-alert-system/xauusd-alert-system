"""Versioned machine-readable signal/setup contract."""
from __future__ import annotations

from enum import Enum
import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel, Field, model_validator


class SignalState(str, Enum):
    WATCH = "watch"
    ARMED = "armed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    NO_TRADE = "no_trade"


class TargetLeg(BaseModel):
    price: float
    close_ratio: float = Field(gt=0.0, le=1.0)
    label: str | None = None


class SignalSpec(BaseModel):
    schema_version: int = 1
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    strategy_version: str
    asset_key: str
    broker_symbol: str | None = None
    direction: str  # long | short | no_trade
    state: SignalState
    setup_timeframe: str
    context_timeframe: str
    created_at_utc: int
    expires_at_utc: int | None = None
    zone_low: float | None = None
    zone_high: float | None = None
    stop_price: float | None = None
    targets: list[TargetLeg] = Field(default_factory=list)
    confirmation_predicates: list[str] = Field(default_factory=list)
    confirmed_by: str | None = None
    confirmation_time_utc: int | None = None
    model_hash: str | None = None
    config_hash: str
    feature_snapshot_hash: str | None = None
    source_channel: str = "internal_pipeline"
    source_message_id: str | None = None
    published_at_utc: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self):
        if self.direction not in {"long", "short", "no_trade"}:
            raise ValueError("direction must be long, short or no_trade")
        if self.zone_low is not None and self.zone_high is not None and self.zone_low > self.zone_high:
            raise ValueError("zone_low must be <= zone_high")
        if self.targets and sum(t.close_ratio for t in self.targets) > 1.000001:
            raise ValueError("target close ratios cannot exceed 1.0")
        if self.state == SignalState.CONFIRMED and not self.confirmed_by:
            raise ValueError("confirmed signals require confirmed_by")
        if self.direction == "no_trade" and self.state != SignalState.NO_TRADE:
            raise ValueError("no_trade direction requires no_trade state")
        return self

    def canonical_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def publish_latency_seconds(self) -> int | None:
        if self.published_at_utc is None:
            return None
        return int(self.published_at_utc - self.created_at_utc)
