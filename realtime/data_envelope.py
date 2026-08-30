"""
DataEnvelope / freshness contract (web-UI spec §6.3 and §13 Wave 0).

Every dashboard payload that carries a timestamped value must expose the same
honesty fields:

* ``source``            - where the value came from (e.g. "mt5_account").
* ``mode``              - deployment/data mode (never inferred as live).
* ``as_of_utc_ms``      - epoch ms of the underlying observation (None = none).
* ``freshness_status``  - fresh | stale | offline | waiting | error.
* ``ingest_lag_ms``     - now - as_of; None when there is no observation.
* ``coverage``          - optional completeness (0..1 or None).
* ``last_successful_at_utc_ms`` - last time the source produced data.

Thresholds follow the spec: fresh <= 5s, stale <= 60s, offline beyond that,
``waiting`` when the source has never produced an observation, ``error`` when
the producer itself failed.

Rule enforced by tests: an unavailable source must never become a numeric
fallback (no $100,000, no neutral 0.50, no random chart).
"""

from __future__ import annotations

import time
from typing import Any, Literal

FreshnessStatus = Literal["fresh", "stale", "offline", "waiting", "error"]

FRESH_AFTER_MS = 5_000  # spec: < 5s -> green "fresh"
STALE_AFTER_MS = 60_000  # spec: 5-60s -> amber "stale"; > 60s -> "offline"

VALID_STATUSES = frozenset(FreshnessStatus.__args__)  # type: ignore[attr-defined]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def freshness_status(
    last_activity_ms: int | None,
    now: int | None = None,
    *,
    fresh_after_ms: int = FRESH_AFTER_MS,
    stale_after_ms: int = STALE_AFTER_MS,
) -> str:
    """Map a last-observation timestamp to the spec's freshness status.

    ``None`` (source never produced data) -> ``waiting``. A timestamp in the
    future (clock skew) is treated as fresh rather than offline.
    """
    if last_activity_ms is None:
        return "waiting"
    age = int(now_ms() if now is None else now) - int(last_activity_ms)
    if age < 0:
        return "fresh"
    if age <= fresh_after_ms:
        return "fresh"
    if age <= stale_after_ms:
        return "stale"
    return "offline"


def freshness_fields(
    last_activity_ms: int | None,
    *,
    source: str,
    mode: str,
    coverage: float | None = None,
    now: int | None = None,
    fresh_after_ms: int = FRESH_AFTER_MS,
    stale_after_ms: int = STALE_AFTER_MS,
    freshness: str | None = None,
) -> dict[str, Any]:
    """The standard freshness keys merged into any payload.

    ``freshness`` overrides the computed status, e.g. ``offline`` when the
    producer is expected but unreachable (spec: "producer не отвечает") even
    though no observation timestamp exists yet.
    """
    now_value = int(now_ms() if now is None else now)
    status = (
        freshness
        if freshness is not None
        else freshness_status(
            last_activity_ms,
            now_value,
            fresh_after_ms=fresh_after_ms,
            stale_after_ms=stale_after_ms,
        )
    )
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid freshness status {status!r}")
    return {
        "source": source,
        "mode": mode,
        "as_of_utc_ms": None if last_activity_ms is None else int(last_activity_ms),
        "freshness_status": status,
        "ingest_lag_ms": None if last_activity_ms is None else max(0, now_value - int(last_activity_ms)),
        "coverage": coverage,
        "last_successful_at_utc_ms": None if last_activity_ms is None else int(last_activity_ms),
    }


def stamp(
    payload: dict[str, Any],
    *,
    last_activity_ms: int | None,
    source: str,
    mode: str,
    coverage: float | None = None,
    now: int | None = None,
    fresh_after_ms: int = FRESH_AFTER_MS,
    stale_after_ms: int = STALE_AFTER_MS,
    freshness: str | None = None,
) -> dict[str, Any]:
    """Merge freshness keys into a payload without dropping existing fields."""
    merged = dict(payload)
    merged.update(
        freshness_fields(
            last_activity_ms,
            source=source,
            mode=mode,
            coverage=coverage,
            now=now,
            fresh_after_ms=fresh_after_ms,
            stale_after_ms=stale_after_ms,
            freshness=freshness,
        )
    )
    return merged


def error_payload(*, source: str, mode: str, reason: str, now: int | None = None) -> dict[str, Any]:
    """Explicit error state; never carries a stale value as current."""
    return {
        "available": False,
        "source": source,
        "mode": mode,
        "freshness_status": "error",
        "as_of_utc_ms": None,
        "ingest_lag_ms": None,
        "coverage": None,
        "last_successful_at_utc_ms": None,
        "reason": reason,
    }
