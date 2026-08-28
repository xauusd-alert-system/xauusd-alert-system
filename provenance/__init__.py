"""Provenance package (ТЗ 8.7).

Единый модуль provenance: типизированная спека ``ProvenanceRecordV2``,
SQLite-каталог ``ProvenanceStore``, ``verify_record`` c TTL-проверкой
(P2-51) и FastAPI-роутер с bulk-аудитом (P2-3).

Обёртка/расширение существующего provenance (P1.6):
хеширование переиспользует ``execution.provenance.sha256_hex``;
адаптер ``record_from_group_row`` каталогизирует записи из существующего
``data.trade_group_store`` без изменения его семантики.
"""

from provenance.spec import (
    PROVENANCE_V2_SCHEMA_VERSION,
    REQUIRED_RECORD_FIELDS,
    ProvenanceRecordV2,
    record_from_group_row,
)
from provenance.store import PROVENANCE_RECORDS_TABLE, ProvenanceStore
from provenance.verifier import (
    DEFAULT_MAX_SNAPSHOT_AGE_MS,
    VerificationResult,
    verify_record,
)

__all__ = [
    "DEFAULT_MAX_SNAPSHOT_AGE_MS",
    "PROVENANCE_RECORDS_TABLE",
    "PROVENANCE_V2_SCHEMA_VERSION",
    "REQUIRED_RECORD_FIELDS",
    "ProvenanceRecordV2",
    "ProvenanceStore",
    "VerificationResult",
    "record_from_group_row",
    "verify_record",
]
