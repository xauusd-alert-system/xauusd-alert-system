"""ProvenanceVerifier (ТЗ 8.7) — проверка полноты, хеша и TTL записи.

Расширяет существующие механизмы, не меняя их:

* полнота — по REQUIRED_RECORD_FIELDS (согласовано с
  ``execution/trade_group.py require_execution_provenance``);
* hash_ok — сверка ``record_hash`` с пересчитанным ``compute_hash()``
  (то же каноническое хеширование, что и у P1.6 provenance);
* TTL (P2-51) — ``provenance.max_snapshot_age_ms`` из конфига
  (config.loader.load_config). Возраст = now - as_of_utc_ms.
  ``age_ok`` осмыслен ТОЛЬКО когда TTL задан в конфиге; без конфига
  ``age_ok=True`` (проверка не настроена — не фейлим аудит).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from provenance.spec import (
    REQUIRED_RECORD_FIELDS,
    ProvenanceRecordV2,
    record_from_group_row,
)

DEFAULT_MAX_SNAPSHOT_AGE_MS = 60_000


@dataclass(frozen=True)
class VerificationResult:
    """Результат verify_record: complete / missing_fields / hash_ok / age_ok."""

    group_id: str
    complete: bool
    missing_fields: list[str] = field(default_factory=list)
    hash_ok: bool = True
    age_ok: bool = True
    snapshot_age_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "complete": self.complete,
            "missing_fields": list(self.missing_fields),
            "hash_ok": self.hash_ok,
            "age_ok": self.age_ok,
            "snapshot_age_ms": self.snapshot_age_ms,
        }


def _load_max_snapshot_age_ms(cfg: dict[str, Any] | None) -> int | None:
    """TTL из конфига (provenance.max_snapshot_age_ms); None = проверка off."""
    if cfg is None:
        try:
            from config.loader import load_config

            cfg = load_config()
        except Exception:
            return None
    value = (cfg or {}).get("provenance", {}).get("max_snapshot_age_ms")
    if value is None:
        return None
    try:
        age = int(value)
    except (TypeError, ValueError):
        return None
    return age if age > 0 else None


def verify_record(
    record: ProvenanceRecordV2 | str,
    *,
    store: Any = None,
    cfg: dict[str, Any] | None = None,
    now_ms: int | None = None,
) -> VerificationResult:
    """Verify one record (by instance or group_id resolved via ``store``).

    ``store`` — ProvenanceStore; when the group_id is absent from the new
    store and ``store.fallback_loader`` is wired (adapter to the legacy
    ``data.trade_group_store.load_group``), the record is catalogized from
    the legacy path instead of failing.
    """
    if isinstance(record, str):
        if store is None:
            raise ValueError("group_id lookup requires a store")
        loaded = store.get(record)
        if loaded is None:
            loader = getattr(store, "fallback_loader", None)
            if loader is None:
                raise KeyError(f"provenance record not found: {record}")
            group = loader(record)
            if group is None:
                raise KeyError(f"provenance record not found: {record}")
            loaded = record_from_group_row(group)
        record = loaded

    # 1. completeness — REQUIRED_RECORD_FIELDS plus non-empty dicts for
    #    broker/cost snapshots (an empty dict is treated as missing).
    missing: list[str] = []
    for key in REQUIRED_RECORD_FIELDS:
        value = getattr(record, key, None)
        if isinstance(value, dict) and not value:
            missing.append(key)
        elif not value:
            missing.append(key)

    # 2. hash integrity — recompute and compare (same canonical hashing as
    #    execution/provenance.py via sha256_hex).
    hash_ok = bool(record.record_hash) and record.record_hash == record.compute_hash()

    # 3. TTL (P2-51) — meaningful only when configured.
    max_age_ms = _load_max_snapshot_age_ms(cfg)
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    age_ms: int | None = None
    if max_age_ms is not None:
        age_ms = int(now_ms) - int(record.as_of_utc_ms)
        age_ok = 0 <= age_ms <= max_age_ms
    else:
        age_ok = True  # no TTL configured -> check not enforced

    return VerificationResult(
        group_id=record.group_id,
        complete=not missing,
        missing_fields=missing,
        hash_ok=hash_ok,
        age_ok=age_ok,
        snapshot_age_ms=age_ms,
    )
