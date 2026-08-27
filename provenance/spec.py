"""ProvenanceRecordV2 — типизированная спека provenance торговых групп (ТЗ 8.7).

Обёртка/расширение существующего контракта P1.6:

* хеширование консистентно с ``execution/provenance.py`` — переиспользуется
  ``sha256_hex`` (канонический JSON: sort_keys + компактные разделители),
  логика НЕ дублируется;
* связь с Feature Store (ТЗ 8.3, план 7.6/Шаг 8): опциональный
  ``feature_snapshot_id``;
* связь с существующим хранением provenance в ``TradeGroupSpec.provenance``
  (execution/trade_group.py) — ``ProvenanceRecordV2.from_trade_group_spec``
  строит запись из spec, ничего в spec не меняя (store каталогизирует
  для аудита, семантика существующих хешей не затрагивается).

Схема v2 (``provenance.v2``)::

    group_id, signal_id, feature_snapshot_id?, config_hash,
    broker_snapshot(dict), cost_snapshot(dict), lineage(dict),
    as_of_utc_ms, executed_at_utc_ms?, schema_version, record_hash
"""
from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field, model_validator

from execution.provenance import sha256_hex

PROVENANCE_V2_SCHEMA_VERSION = "provenance.v2"

#: Поля, обязательные для "полной" lineage-записи (проверяет verifier).
#: feature_snapshot_id опционален — связь с Feature Store включается конфигом.
REQUIRED_RECORD_FIELDS = (
    "group_id",
    "signal_id",
    "config_hash",
    "broker_snapshot",
    "cost_snapshot",
    "record_hash",
)


class TradeGroupProvenanceDict(TypedDict, total=False):
    """ТЗ 7.6: документация ключей legacy dict-провенанса в
    ``TradeGroupSpec.provenance`` (execution/trade_group.py).

    Поле spec.provenance остаётся ``dict[str, Any]`` ради обратной
    совместимости (legacy-тесты/фикстуры), но ожидаемый набор ключей
    зафиксирован здесь как TypedDict — типизация без изменения рантайма.
    Полный набор ключей требуется только перед исполнением
    (``require_execution_provenance``, P1.6 §22/§23).
    """

    # lineage-ссылки на родительские снапшоты
    market_snapshot_id: str
    feature_snapshot_id: str
    model_inference_id: str
    model_hash: str
    profile_id: str
    broker_snapshot_id: str
    cost_snapshot_id: str
    # расширенные снапшоты (опционально)
    broker_snapshot: dict[str, Any]
    cost_snapshot: dict[str, Any]
    # детерминированные хеши (§21): ЧТО / ИЗ ЧЕГО
    geometry_hash: str
    provenance_hash: str
    # источник (P1.6 §37: "unknown" не валиден; §31 paper != mt5)
    source: str


class ProvenanceRecordV2(BaseModel):
    """Аудиторская запись provenance одной торговой группы (ТЗ 8.7)."""

    schema_version: str = PROVENANCE_V2_SCHEMA_VERSION
    group_id: str
    signal_id: str
    feature_snapshot_id: str | None = None
    config_hash: str
    broker_snapshot: dict[str, Any] = Field(default_factory=dict)
    cost_snapshot: dict[str, Any] = Field(default_factory=dict)
    lineage: dict[str, Any] = Field(default_factory=dict)
    as_of_utc_ms: int
    executed_at_utc_ms: int | None = None
    record_hash: str | None = None

    @model_validator(mode="after")
    def _validate_identity(self):
        for field in ("group_id", "signal_id"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"{field} must not be empty")
        if not str(self.config_hash).strip():
            raise ValueError("config_hash must not be empty")
        if self.as_of_utc_ms <= 0:
            raise ValueError("as_of_utc_ms must be positive")
        if self.executed_at_utc_ms is not None and self.executed_at_utc_ms <= 0:
            raise ValueError("executed_at_utc_ms must be positive when present")
        # Детерминизм: hash вычисляется один раз при создании, если не задан
        # явно (явно заданный hash позволяет verifier поймать расхождение).
        if self.record_hash is None:
            self.record_hash = self.compute_hash()
        return self

    # --- hashing (консистентно с execution/provenance.py) --------------------

    def compute_hash(self) -> str:
        """sha256 над каноническим JSON записи (без record_hash).

        Использует ``execution.provenance.sha256_hex`` — тот же канонический
        payload, что у существующих provenance-хешей (P1.6 §21/§24).
        """
        payload = self.model_dump(mode="json", exclude={"record_hash"})
        return sha256_hex(payload)

    # --- адаптеры к существующему хранению -----------------------------------

    @classmethod
    def from_trade_group_spec(cls, spec: Any) -> "ProvenanceRecordV2":
        """Построить запись из ``TradeGroupSpec`` (data/trade_group_store).

        Только чтение spec.provenance — существующие поля и хеши spec
        не изменяются (модуль обёртывает, а не переписывает lineage).
        """
        prov = dict(getattr(spec, "provenance", None) or {})
        broker_snapshot = prov.pop("broker_snapshot", None)
        if not isinstance(broker_snapshot, dict):
            broker_snapshot = ({"broker_snapshot_id": prov.pop(
                "broker_snapshot_id", None)} if prov.get("broker_snapshot_id")
                or "broker_snapshot_id" in prov else {})
        cost_snapshot = prov.pop("cost_snapshot", None)
        if not isinstance(cost_snapshot, dict):
            cost_snapshot = ({"cost_snapshot_id": prov.pop(
                "cost_snapshot_id", None)} if prov.get("cost_snapshot_id")
                or "cost_snapshot_id" in prov else {})
        feature_snapshot_id = prov.pop("feature_snapshot_id", None)
        return cls(
            group_id=str(spec.group_id),
            signal_id=str(spec.signal_id),
            feature_snapshot_id=feature_snapshot_id,
            config_hash=str(spec.config_hash),
            broker_snapshot=broker_snapshot,
            cost_snapshot=cost_snapshot,
            lineage=prov,
            as_of_utc_ms=int(spec.created_at_utc_ms),
        )


def record_from_group_row(group: dict[str, Any]) -> ProvenanceRecordV2:
    """Адаптер: строка ``data.trade_group_store.load_group`` -> запись v2.

    Fallback-путь для верификатора: если записи нет в новом store,
    provenance каталогизируется из существующего trade_group_store.
    """
    spec = group.get("spec")
    if spec is None:
        raise ValueError("group row has no spec")
    return ProvenanceRecordV2.from_trade_group_spec(spec)
