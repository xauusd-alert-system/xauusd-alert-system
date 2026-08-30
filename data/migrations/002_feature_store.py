"""Migration 002 — ``feature_store``: create the feature_snapshots table (ТЗ 8.3).

The Feature Store (features/feature_store.py) catalogs computed feature
snapshots for reproducibility and provenance linkage. This migration creates
its table and indexes so the schema is versioned and recorded in
``schema_migrations``; the store itself also runs an idempotent
``CREATE TABLE IF NOT EXISTS`` so it works on fresh databases without
waiting for the runner (both paths use the identical DDL).

Schema::

    feature_snapshots(
        snapshot_id          TEXT PRIMARY KEY,
        symbol               TEXT NOT NULL,
        timeframe            TEXT NOT NULL,
        bar_ts_utc_ms        INTEGER NOT NULL,
        feature_set_version  TEXT NOT NULL,
        features_json        TEXT NOT NULL,
        computed_at_utc_ms   INTEGER NOT NULL,
        UNIQUE(symbol, timeframe, bar_ts_utc_ms, feature_set_version)
    )
    + idx_feature_snapshots_lookup(symbol, timeframe, feature_set_version,
                                   bar_ts_utc_ms)
    + idx_feature_snapshots_ts(bar_ts_utc_ms)
"""

VERSION = 2
NAME = "feature_store"

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feature_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_ts_utc_ms INTEGER NOT NULL,
    feature_set_version TEXT NOT NULL,
    features_json TEXT NOT NULL,
    computed_at_utc_ms INTEGER NOT NULL,
    UNIQUE(symbol, timeframe, bar_ts_utc_ms, feature_set_version)
)
"""

INDEX_SQL = (
    """
    CREATE INDEX IF NOT EXISTS idx_feature_snapshots_lookup
    ON feature_snapshots(symbol, timeframe, feature_set_version,
                         bar_ts_utc_ms)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_feature_snapshots_ts
    ON feature_snapshots(bar_ts_utc_ms)
    """,
)


def apply(conn) -> None:
    conn.execute(TABLE_SQL)
    for index_sql in INDEX_SQL:
        conn.execute(index_sql)
