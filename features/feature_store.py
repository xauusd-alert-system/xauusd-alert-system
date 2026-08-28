"""Feature Store (ТЗ 8.3).

Единое хранилище ВЫЧИСЛЕННЫХ фичей со снапшотами для воспроизводимости.

Ключевые свойства:

* Feature Store НЕ пересчитывает фичи по-другому: он каталогизирует и
  кэширует значения, полученные либо из переданного ``compute_fn``
  (существующий feature-builder), либо из готового dict ``features``;
* каждый снапшот идентифицируется детерминированным ``snapshot_id`` —
  SHA-256 хешем от ``symbol|timeframe|bar_ts|feature_set_version|features_hash``.
  Одинаковые входы всегда дают одинаковый snapshot_id (воспроизводимость);
* ``feature_set_version`` (см. ``features.FEATURES_SCHEMA_VERSION``) изолирует
  наборы фичей разных версий: строки с разными версиями никогда не
  пересекаются (UNIQUE-контракт включает версию);
* хранилище живёт в SQLite рядом с candles (тот же движок, что
  ``data/storage.py``); схема создаётся миграцией ``002_feature_store`` и
  дублируется идемпотентным ``CREATE TABLE IF NOT EXISTS`` в этом модуле.

Таблица ``feature_snapshots``::

    snapshot_id TEXT PRIMARY KEY
    symbol TEXT NOT NULL
    timeframe TEXT NOT NULL
    bar_ts_utc_ms INTEGER NOT NULL          -- ms (UTC), закрытие бара
    feature_set_version TEXT NOT NULL
    features_json TEXT NOT NULL             -- canonical JSON (sort_keys)
    computed_at_utc_ms INTEGER NOT NULL
    UNIQUE(symbol, timeframe, bar_ts_utc_ms, feature_set_version)
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Mapping

from data.storage import get_connection
from features import FEATURES_SCHEMA_VERSION

FEATURE_SNAPSHOTS_TABLE = "feature_snapshots"

FEATURE_SNAPSHOTS_SQL = """
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

FEATURE_SNAPSHOTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_feature_snapshots_lookup "
    "ON feature_snapshots(symbol, timeframe, feature_set_version, "
    "bar_ts_utc_ms);",
    "CREATE INDEX IF NOT EXISTS idx_feature_snapshots_ts ON feature_snapshots(bar_ts_utc_ms);",
)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def canonical_features_json(features: Mapping[str, Any]) -> str:
    """Canonical JSON for a feature dict (stable ordering, compact separators)."""
    return json.dumps(dict(features), sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> str:
    # numpy scalars / NaN etc. — str() keeps the snapshot serializable and
    # deterministic for the same input values.
    return str(value)


def features_hash(features: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_features_json(features).encode()).hexdigest()


def compute_snapshot_id(
    symbol: str,
    timeframe: str,
    bar_ts_utc_ms: int,
    feature_set_version: str,
    feat_hash: str,
) -> str:
    """Deterministic snapshot_id: SHA-256(symbol|tf|bar_ts|version|features_hash)."""
    seed = f"{symbol}|{timeframe}|{int(bar_ts_utc_ms)}|{feature_set_version}|{feat_hash}"
    return hashlib.sha256(seed.encode()).hexdigest()


class FeatureStore:
    """SQLite-backed store of computed feature snapshots."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        conn = get_connection(self.db_path)
        try:
            conn.execute(FEATURE_SNAPSHOTS_SQL)
            for index_sql in FEATURE_SNAPSHOTS_INDEXES:
                conn.execute(index_sql)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # write path
    # ------------------------------------------------------------------
    def compute_and_store(
        self,
        symbol: str,
        timeframe: str,
        bar_ts: int,
        compute_fn: Callable[[], Mapping[str, Any]] | None = None,
        features: Mapping[str, Any] | None = None,
        feature_set_version: str = FEATURES_SCHEMA_VERSION,
    ) -> tuple[str, dict[str, Any]]:
        """Compute (or accept ready-made) features and persist a snapshot.

        Exactly one of ``compute_fn`` / ``features`` must be provided:
        ``compute_fn`` is invoked to obtain the feature dict (no re-computation
        of anything else — the store never alters values); ``features`` passes
        a pre-computed dict through unchanged.

        Returns ``(snapshot_id, features_dict)``.
        """
        if (compute_fn is None) == (features is None):
            raise ValueError("provide exactly one of compute_fn or features")
        if compute_fn is not None:
            computed = compute_fn()
            if computed is None:
                raise ValueError("compute_fn returned None; expected a mapping")
            features = dict(computed)
        else:
            features = dict(features or {})

        feat_hash = features_hash(features)
        snapshot_id = compute_snapshot_id(
            symbol=symbol,
            timeframe=timeframe,
            bar_ts_utc_ms=int(bar_ts),
            feature_set_version=feature_set_version,
            feat_hash=feat_hash,
        )

        conn = get_connection(self.db_path)
        try:
            conn.execute(FEATURE_SNAPSHOTS_SQL)
            conn.execute(
                f"INSERT INTO {FEATURE_SNAPSHOTS_TABLE} "
                f"(snapshot_id, symbol, timeframe, bar_ts_utc_ms, "
                f" feature_set_version, features_json, computed_at_utc_ms) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT(symbol, timeframe, bar_ts_utc_ms, "
                f"feature_set_version) DO UPDATE SET "
                f"snapshot_id=excluded.snapshot_id, "
                f"features_json=excluded.features_json, "
                f"computed_at_utc_ms=excluded.computed_at_utc_ms",
                (
                    snapshot_id,
                    str(symbol),
                    str(timeframe),
                    int(bar_ts),
                    str(feature_set_version),
                    canonical_features_json(features),
                    _now_ms(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return snapshot_id, features

    # ------------------------------------------------------------------
    # read path
    # ------------------------------------------------------------------
    def get(self, snapshot_id: str) -> dict[str, Any] | None:
        """Return a snapshot record by id (features parsed), or None."""
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                f"SELECT snapshot_id, symbol, timeframe, bar_ts_utc_ms, "
                f"feature_set_version, features_json, computed_at_utc_ms "
                f"FROM {FEATURE_SNAPSHOTS_TABLE} WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_dict(row) if row else None

    def get_latest(
        self,
        symbol: str,
        timeframe: str,
        feature_set_version: str = FEATURES_SCHEMA_VERSION,
    ) -> dict[str, Any] | None:
        """Most recent snapshot for (symbol, timeframe, version), or None."""
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                f"SELECT snapshot_id, symbol, timeframe, bar_ts_utc_ms, "
                f"feature_set_version, features_json, computed_at_utc_ms "
                f"FROM {FEATURE_SNAPSHOTS_TABLE} "
                f"WHERE symbol = ? AND timeframe = ? AND feature_set_version = ? "
                f"ORDER BY bar_ts_utc_ms DESC LIMIT 1",
                (str(symbol), str(timeframe), str(feature_set_version)),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_dict(row) if row else None

    def get_range(
        self,
        symbol: str,
        timeframe: str,
        from_ts: int,
        to_ts: int,
        feature_set_version: str = FEATURES_SCHEMA_VERSION,
    ) -> list[dict[str, Any]]:
        """Snapshots with from_ts <= bar_ts_utc_ms <= to_ts, ordered by bar ts."""
        conn = get_connection(self.db_path)
        try:
            rows = conn.execute(
                f"SELECT snapshot_id, symbol, timeframe, bar_ts_utc_ms, "
                f"feature_set_version, features_json, computed_at_utc_ms "
                f"FROM {FEATURE_SNAPSHOTS_TABLE} "
                f"WHERE symbol = ? AND timeframe = ? AND feature_set_version = ? "
                f"AND bar_ts_utc_ms >= ? AND bar_ts_utc_ms <= ? "
                f"ORDER BY bar_ts_utc_ms ASC",
                (str(symbol), str(timeframe), str(feature_set_version), int(from_ts), int(to_ts)),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        return {
            "snapshot_id": row[0],
            "symbol": row[1],
            "timeframe": row[2],
            "bar_ts_utc_ms": row[3],
            "feature_set_version": row[4],
            "features": json.loads(row[5]),
            "computed_at_utc_ms": row[6],
        }
