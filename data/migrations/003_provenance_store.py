"""Migration 003 — ``provenance_store``: аудиторский каталог provenance (ТЗ 8.7).

ProvenanceStore (provenance/store.py) каталогизирует provenance торговых
групп для bulk-аудита (P2-3) и TTL-проверок (P2-51). Это ДОПОЛНИТЕЛЬНОЕ
хранение: существующее сохранение provenance в ``trade_groups``
(data/trade_group_store.py) не меняется.

Схема::

    provenance_records(
        group_id             TEXT PRIMARY KEY,
        signal_id            TEXT NOT NULL,
        feature_snapshot_id  TEXT,
        config_hash          TEXT NOT NULL,
        broker_snapshot_json TEXT NOT NULL,
        cost_snapshot_json   TEXT NOT NULL,
        lineage_json         TEXT NOT NULL,
        as_of_utc_ms         INTEGER NOT NULL,
        executed_at_utc_ms   INTEGER,
        record_hash          TEXT NOT NULL,
        schema_version       TEXT NOT NULL,
        recorded_at_utc_ms   INTEGER NOT NULL
    )
    + idx_provenance_records_signal(signal_id)
    + idx_provenance_records_as_of(as_of_utc_ms)
"""

VERSION = 3
NAME = "provenance_store"

TABLE_SQL = """
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

INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_provenance_records_signal ON provenance_records(signal_id)",
    "CREATE INDEX IF NOT EXISTS idx_provenance_records_as_of ON provenance_records(as_of_utc_ms)",
)


def apply(conn) -> None:
    conn.execute(TABLE_SQL)
    for index_sql in INDEX_SQL:
        conn.execute(index_sql)
