"""
Execution contracts v1 — SignalIntent / ExecutionEvent / EventEnvelope.

These models are the wire contract between the Python trading system, the MQL5
observer EA (``mql5/SignalDeskObserver``) and the server-side ledger
(``data/ledger_events.py`` + ``POST /api/ledger/ingest``).

Guarantees:

* ``SignalIntent`` is created by Python **before** ``order_send`` and persisted
  in the source journal (``data/intent_ledger.py``). Its ``intent_id`` is the
  correlation key carried through the broker order comment (short form) and
  into every downstream execution fact.
* ``ExecutionEvent`` is an immutable fact. ``event_id`` is **deterministic**
  from (source, account fingerprint, transaction kind, transaction id), so
  repeated delivery of the same fact (outbox retry, restart reconciliation)
  always produces the same id and the server upsert is idempotent.
* MQL5 has no SHA-256 builtin, so the observer emits the *canonical id string*
  directly; Python emits the sha256 hex of the same canonical string. Both are
  deterministic and the server treats ``event_id`` as an opaque primary key.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 1

# ТЗ 10.4 / P2-50: explicit wire protocol version. Producers stamp
# ``protocol_version`` on the ingest envelope; receivers reject unknown
# versions and treat a MISSING field as version 1 (compatibility with
# observers deployed before the field existed).
OBSERVER_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = frozenset({1})
DEFAULT_PROTOCOL_VERSION = 1


def check_protocol_version(raw: dict) -> tuple[bool, str, int]:
    """Validate ``protocol_version`` on a raw ingest envelope dict.

    Returns ``(ok, error, effective_version)``. A missing field is
    accepted as :data:`DEFAULT_PROTOCOL_VERSION` (v1 compatibility);
    a present-but-unknown version is rejected.
    """
    version = raw.get("protocol_version", None)
    if version is None:
        return True, "", DEFAULT_PROTOCOL_VERSION
    try:
        version = int(version)
    except (TypeError, ValueError):
        return False, f"protocol_version must be an integer, got {version!r}", 0
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        return False, (
            f"unsupported protocol_version {version}; "
            f"supported: {sorted(SUPPORTED_PROTOCOL_VERSIONS)}"
        ), version
    return True, "", version

# Sources that may publish execution facts.
Source = Literal["mt5_observer", "mt5_python_sender", "ledger_bridge", "preflight_tool"]

# Account trade modes (MT5 ACCOUNT_TRADE_MODE_*).
AccountMode = Literal["demo", "real", "contest"]

# How a fact was obtained. "request" = direct response of an order request,
# "probe" = controlled minimum-lot demo probe, "passive" = observed passively
# (OnTradeTransaction), "history_reconciled" = reconstructed from history after
# a restart, "preflight" = OrderCheck-style validation result.
Precision = Literal["probe", "passive", "history_reconciled", "request", "preflight"]

# Deployment modes (must match config.deployment.mode values).
IntentMode = Literal[
    "simulation", "research", "paper", "human_confirmed", "demo_systematic", "live_systematic",
]

EXECUTION_EVENT_TYPES = frozenset({
    "deal_added",            # broker deal fact (entry/exit/partial)
    "order_history_added",   # order reached history (filled/closed/cancelled)
    "position_modified",     # position SL/TP/volume change observed
    "request_result",        # direct response of an order_send (Python sender)
    "preflight_checked",     # OrderCheck-style preflight fact (diagnostics only)
    "execution_reconciled",  # history reconciliation summary (restart)
    "intent_created",        # SignalIntent persisted before order_send
    "health_heartbeat",      # observer liveness (uptime, pending outbox count)
})

VALID_PRECISIONS = frozenset(Precision.__args__)  # type: ignore[attr-defined]
VALID_SOURCES = frozenset(Source.__args__)  # type: ignore[attr-defined]
VALID_ACCOUNT_MODES = frozenset(AccountMode.__args__)  # type: ignore[attr-defined]
VALID_INTENT_MODES = frozenset(IntentMode.__args__)  # type: ignore[attr-defined]


def canonical_event_id_string(
    source: str,
    account_fingerprint: str,
    transaction_kind: str,
    transaction_id: str,
) -> str:
    """Deterministic id *string* for an execution fact.

    Same inputs always produce the same string. The MQL5 observer emits this
    string verbatim as ``event_id``; Python hashes it (see
    :func:`execution_event_id`). Never put free-form text in
    ``transaction_id`` — it must be a broker/terminal-supplied unique id
    (deal ticket, order ticket, intent id, ...).
    """
    if "|" in source or "|" in account_fingerprint:
        raise ValueError("source/account_fingerprint must not contain '|'")
    if "|" in transaction_kind or "|" in transaction_id:
        raise ValueError("transaction_kind/transaction_id must not contain '|'")
    return f"{source}|{account_fingerprint}|{transaction_kind}|{transaction_id}"


def execution_event_id(
    source: str,
    account_fingerprint: str,
    transaction_kind: str,
    transaction_id: str,
) -> str:
    """sha256 hex of :func:`canonical_event_id_string` (Python-side event ids)."""
    canonical = canonical_event_id_string(source, account_fingerprint, transaction_kind, transaction_id)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def account_fingerprint(account_mode: str, account_login: int | str) -> str:
    """Compact, collision-safe account fingerprint used in event ids."""
    return f"{account_mode}:{account_login}"


def new_intent_id() -> str:
    """Random 32-char hex intent id (correlation key, not a broker id)."""
    return uuid.uuid4().hex


class SignalIntent(BaseModel):
    """Immutable, Python-created intent recorded **before** ``order_send``.

    Contract fields (plan: "Контракты, которые нужно зафиксировать до кода"):
    intent_id, asset_key, broker_symbol, side, requested_volume, entry/sl/tp
    geometry, model_version, feature_manifest_hash, config_hash, mode,
    created_at_utc_ms, magic_number, source.
    """

    schema_version: int = SCHEMA_VERSION
    intent_id: str = Field(default_factory=new_intent_id)
    asset_key: str
    broker_symbol: str
    side: Literal["long", "short"]
    requested_volume: float = Field(gt=0.0)
    entry_price: float
    sl_price: float | None = None
    tp_price: float | None = None
    model_version: str | None = None
    feature_manifest_hash: str | None = None
    config_hash: str | None = None
    mode: IntentMode
    magic_number: int = Field(default=0, ge=0)
    source: Source = "mt5_python_sender"
    signal_id: str | None = None
    created_at_utc_ms: int

    @model_validator(mode="after")
    def validate_geometry(self):
        if self.sl_price is not None and self.tp_price is not None:
            direction = 1.0 if self.side == "long" else -1.0
            if direction * (self.tp_price - self.sl_price) <= 0.0:
                raise ValueError("tp_price must be beyond sl_price for the intent side")
        return self

    def canonical_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", exclude={"intent_id"}),
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ExecutionEvent(BaseModel):
    """One immutable execution fact (plan: ``ExecutionEvent v1``)."""

    schema_version: int = SCHEMA_VERSION
    event_id: str
    event_type: str
    intent_id: str | None = None
    source: Source = "mt5_observer"
    account_mode: AccountMode = "demo"
    broker_symbol: str
    asset_key: str | None = None
    magic_number: int | None = None
    order_ticket: int | None = None
    deal_ticket: int | None = None
    position_ticket: int | None = None
    deal_time_msc: int | None = None
    retcode: int | None = None
    requested_price: float | None = None
    fill_price: float | None = None
    filled_volume: float | None = None
    volume_requested: float | None = None
    spread_points: float | None = None
    commission: float | None = None
    swap: float | None = None
    latency_ms: int | None = None
    precision: Precision = "passive"
    received_at_utc_ms: int
    reason: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_event(self):
        if self.event_type not in EXECUTION_EVENT_TYPES:
            raise ValueError(
                f"unsupported execution event type {self.event_type!r}; "
                f"expected one of {sorted(EXECUTION_EVENT_TYPES)}"
            )
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        return self

    def canonical_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", exclude={"event_id", "received_at_utc_ms"}),
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EventEnvelope(BaseModel):
    """Batch of execution facts as delivered over HTTPS to /api/ledger/ingest."""

    schema_version: int = SCHEMA_VERSION
    producer: Source = "mt5_observer"
    account_fingerprint: str
    batch_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    sent_at_utc_ms: int
    events: list[ExecutionEvent] = Field(min_length=1)


def build_signal_intent(
    *,
    asset_key: str,
    broker_symbol: str,
    side: str,
    requested_volume: float,
    entry_price: float,
    sl_price: float | None = None,
    tp_price: float | None = None,
    model_version: str | None = None,
    feature_manifest_hash: str | None = None,
    config_hash: str | None = None,
    mode: str,
    magic_number: int,
    signal_id: str | None = None,
    created_at_utc_ms: int | None = None,
    intent_id: str | None = None,
) -> SignalIntent:
    """Build a validated SignalIntent; safe to call before any broker request."""
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    if mode not in VALID_INTENT_MODES:
        raise ValueError(f"unsupported intent mode {mode!r}")
    return SignalIntent(
        intent_id=intent_id or new_intent_id(),
        asset_key=asset_key,
        broker_symbol=broker_symbol,
        side=side,  # type: ignore[arg-type]
        requested_volume=float(requested_volume),
        entry_price=float(entry_price),
        sl_price=None if sl_price is None else float(sl_price),
        tp_price=None if tp_price is None else float(tp_price),
        model_version=model_version,
        feature_manifest_hash=feature_manifest_hash,
        config_hash=config_hash,
        mode=mode,  # type: ignore[arg-type]
        magic_number=int(magic_number),
        signal_id=signal_id,
        created_at_utc_ms=int(created_at_utc_ms) if created_at_utc_ms is not None else 0,
    )


def execution_event_from_dict(payload: dict[str, Any]) -> ExecutionEvent:
    """Parse one fact dict (from JSONL outbox / HTTP body) with validation."""
    return ExecutionEvent.model_validate(payload)


def event_envelope_from_dict(payload: dict[str, Any]) -> EventEnvelope:
    """Parse an ingest envelope with validation."""
    return EventEnvelope.model_validate(payload)
