"""ProvenanceStore (ТЗ 8.7) — SQLite-каталог provenance торговых групп.

Каталогизирует provenance для аудита (bulk-аудит P2-3, TTL P2-51).
НЕ заменяет существующее сохранение provenance в ``trade_groups``
(data/trade_group_store.py): executor по-прежнему пишет spec+provenance
туда, а этот store — параллельный аудиторский индекс за конфигом
``provenance.store.enabled`` (по умолчанию выключен, fail-open).

Миграция схемы — ``data/migrations/003_provenance_store.py``; для свежих
баз store дополнительно выполняет идемпотентный ``CREATE TABLE IF NOT
EXISTS`` (идентичный DDL — тот же паттерн, что у Feature Store, Шаг 8).
"""

from __future__ import annotations

import json
import time
from typing import Any

from data.storage import get_connection
from provenance.spec import ProvenanceRecordV2, record_from_group_row

PROVENANCE_RECORDS_TABLE = "provenance_records"

PROVENANCE_RECORDS_SQL = """
CREATE TABLE IF NOT EXISTS provenance_records (
    group_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    feature_snapshot_id TEXT,
    config_hash TEXT NOT NULL,
    broker_snapshot_json TEXT NOT NULL,
    cost_snapshot_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    as_of_utc_ms INTEGER NOT NULL,
    executed_at_utc_ms INTEGER,
    record_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    recorded_at_utc_ms INTEGER NOT NULL
)
"""

PROVENANCE_RECORDS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_provenance_records_signal ON provenance_records(signal_id);",
    "CREATE INDEX IF NOT EXISTS idx_provenance_records_as_of ON provenance_records(as_of_utc_ms);",
)

_COLUMNS = (
    "group_id",
    "signal_id",
    "feature_snapshot_id",
    "config_hash",
    "broker_snapshot_json",
    "cost_snapshot_json",
    "lineage_json",
    "as_of_utc_ms",
    "executed_at_utc_ms",
    "record_hash",
    "schema_version",
    "recorded_at_utc_ms",
)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def resolve_store_db_path(cfg: dict[str, Any] | None) -> str:
    """Store DB path: env PROVENANCE_STORE_DB_PATH > config
    provenance.store.db_path > config general.db_path (candles DB)."""
    import os

    env = os.environ.get("PROVENANCE_STORE_DB_PATH")
    if env:
        return env
    cfg = cfg or {}
    configured = ((cfg.get("provenance") or {}).get("store") or {}).get("db_path")
    if configured:
        return str(configured)
    return str(cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite"))


class ProvenanceStore:
    """SQLite-backed audit catalog of trade-group provenance records."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        conn = get_connection(self.db_path)
        try:
            conn.execute(PROVENANCE_RECORDS_SQL)
            for index_sql in PROVENANCE_RECORDS_INDEXES:
                conn.execute(index_sql)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # write path
    # ------------------------------------------------------------------
    def save(self, record: ProvenanceRecordV2) -> str:
        """Insert or update one record (upsert by group_id). Returns group_id."""
        if not isinstance(record, ProvenanceRecordV2):
            record = ProvenanceRecordV2.model_validate(record)
        conn = get_connection(self.db_path)
        try:
            conn.execute(PROVENANCE_RECORDS_SQL)
            for index_sql in PROVENANCE_RECORDS_INDEXES:
                conn.execute(index_sql)
            conn.execute(
                f"""INSERT INTO {PROVENANCE_RECORDS_TABLE}
                    ({", ".join(_COLUMNS)})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        signal_id=excluded.signal_id,
                        feature_snapshot_id=excluded.feature_snapshot_id,
                        config_hash=excluded.config_hash,
                        broker_snapshot_json=excluded.broker_snapshot_json,
                        cost_snapshot_json=excluded.cost_snapshot_json,
                        lineage_json=excluded.lineage_json,
                        as_of_utc_ms=excluded.as_of_utc_ms,
                        executed_at_utc_ms=excluded.executed_at_utc_ms,
                        record_hash=excluded.record_hash,
                        schema_version=excluded.schema_version,
                        recorded_at_utc_ms=excluded.recorded_at_utc_ms""",
                (
                    record.group_id,
                    record.signal_id,
                    record.feature_snapshot_id,
                    record.config_hash,
                    _canonical_json(record.broker_snapshot),
                    _canonical_json(record.cost_snapshot),
                    _canonical_json(record.lineage),
                    int(record.as_of_utc_ms),
                    record.executed_at_utc_ms,
                    record.record_hash,
                    record.schema_version,
                    _now_ms(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return record.group_id

    def save_from_group_row(self, group: dict[str, Any]) -> str:
        """Каталогизация из строки ``data.trade_group_store.load_group``
        (адаптер к существующему хранению; spec не изменяется)."""
        return self.save(record_from_group_row(group))

    # ------------------------------------------------------------------
    # read path
    # ------------------------------------------------------------------
    def get(self, group_id: str) -> ProvenanceRecordV2 | None:
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM {PROVENANCE_RECORDS_TABLE} WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_record(row) if row else None

    def get_range(self, from_ts: int, to_ts: int) -> list[ProvenanceRecordV2]:
        """Records with from_ts <= as_of_utc_ms <= to_ts, ordered by as_of."""
        conn = get_connection(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM {PROVENANCE_RECORDS_TABLE} "
                f"WHERE as_of_utc_ms >= ? AND as_of_utc_ms <= ? "
                f"ORDER BY as_of_utc_ms ASC, group_id ASC",
                (int(from_ts), int(to_ts)),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row) -> ProvenanceRecordV2:
        return ProvenanceRecordV2(
            group_id=row[0],
            signal_id=row[1],
            feature_snapshot_id=row[2],
            config_hash=row[3],
            broker_snapshot=json.loads(row[4] or "{}"),
            cost_snapshot=json.loads(row[5] or "{}"),
            lineage=json.loads(row[6] or "{}"),
            as_of_utc_ms=row[7],
            executed_at_utc_ms=row[8],
            record_hash=row[9],
            schema_version=row[10],
        )
