"""Python -> MQL5 signal bridge over SQLite (task T-16; MQL5 book 7.9).

Division of responsibility per the TZ: **Python owns data + ML inference,
MQL5 owns execution and alerts**. The MetaTrader5 Python package has no
event model and no indicator access (book p. 1998-2000), so the bridge is a
plain SQLite table in the terminal's ``MQL5\\Files`` directory:

    Python:  data/inference -> write_signal(...)  [status = 'new']
    MQL5 EA: SignalBridge.mqh polls pending 'new' rows, executes them via
             TradeExecutor (T-05) and flips the status to
             'consumed'/'executed'/'skipped' with the resulting ticket.

Autotrading from Python stays DISABLED (terminal option; the bridge never
calls order_send - error 10027 stays a feature, not a bug). Writes are
idempotent on ``intent_id`` (a retry touches only ``updated_at_utc``,
keeping any status the EA already set) and every row carries the feature-vector
hash so the full signal trace (T-22) can join features -> decision -> execution.

Schema is versioned; the MQL5 side must refuse a mismatched
``schema_version`` (fail-closed, like the observer's wire contract).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_TABLE = "ml_signals"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {DEFAULT_TABLE} (
    intent_id       TEXT    PRIMARY KEY,
    created_at_utc  INTEGER NOT NULL,
    asset           TEXT    NOT NULL,
    direction       INTEGER NOT NULL,        -- +1 long, -1 short
    probability     REAL    NOT NULL,
    entry_price     REAL,
    sl_price        REAL,
    tp_price        REAL,
    horizon_bars    INTEGER,
    expires_at_utc  INTEGER,
    status          TEXT    NOT NULL DEFAULT 'new',
    updated_at_utc  INTEGER NOT NULL,
    features_hash   TEXT,
    comment         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ml_signals_status ON {DEFAULT_TABLE} (status);
CREATE TABLE IF NOT EXISTS bridge_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO bridge_meta (key, value)
VALUES ('schema_version', '{SCHEMA_VERSION}');
"""

STATUSES = ("new", "consumed", "executed", "skipped", "failed", "expired")


@dataclass
class SignalIntent:
    intent_id: str
    asset: str
    direction: int
    probability: float
    entry_price: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    horizon_bars: int | None = None
    expires_at_utc: int | None = None
    features_hash: str | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError(f"direction must be +1/-1, got {self.direction}")
        if not 0.0 <= float(self.probability) <= 1.0:
            raise ValueError(f"probability must lie in [0, 1], got {self.probability}")
        if not self.intent_id or not self.asset:
            raise ValueError("intent_id and asset are required")


def default_bridge_path(terminal_files_dir: str | None = None) -> str:
    """SQLite file location: the MT5 terminal's MQL5\\Files (shared with the
    EA) or a local fallback for tests/backtests."""
    if terminal_files_dir:
        return str(Path(terminal_files_dir) / "ml_signal_bridge.sqlite")
    return str(Path("data") / "ml_signal_bridge.sqlite")


class SignalBridgeWriter:
    """Python-side producer of the shared signal table."""

    def __init__(self, db_path: str, table: str = DEFAULT_TABLE,
                 ttl_seconds: int = 3 * 3600):
        self.db_path = str(db_path)
        self.table = table
        self.ttl_seconds = int(ttl_seconds)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SignalBridgeWriter":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def write_signal(self, intent: SignalIntent) -> str:
        """Idempotent write of a new intent (retries keep status intact)."""
        conn = self.connect()
        now = int(time.time())
        expires = intent.expires_at_utc or (now + self.ttl_seconds)
        with conn:
            conn.execute(
                f"INSERT INTO {self.table} "
                "(intent_id, created_at_utc, asset, direction, probability, "
                " entry_price, sl_price, tp_price, horizon_bars, expires_at_utc, "
                " status, updated_at_utc, features_hash, comment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?) "
                "ON CONFLICT(intent_id) DO UPDATE SET "
                "updated_at_utc=excluded.updated_at_utc",
                (intent.intent_id, now, intent.asset, int(intent.direction),
                 float(intent.probability), intent.entry_price, intent.sl_price,
                 intent.tp_price, intent.horizon_bars, expires,
                 now, intent.features_hash, intent.comment))
        return intent.intent_id

    def pending_signals(self, now_utc: int | None = None) -> list[dict]:
        """Rows the EA may still act on: status 'new' and not expired."""
        conn = self.connect()
        now = int(now_utc if now_utc is not None else time.time())
        rows = conn.execute(
            f"SELECT * FROM {self.table} WHERE status = 'new' "
            f"AND (expires_at_utc IS NULL OR expires_at_utc > ?) "
            f"ORDER BY created_at_utc", (now,)).fetchall()
        cols = [d[0] for d in conn.execute(
            f"SELECT * FROM {self.table} LIMIT 1").description]
        return [dict(zip(cols, r)) for r in rows]

    def mark(self, intent_id: str, status: str, comment: str | None = None,
             now_utc: int | None = None) -> bool:
        """Status transition (also called by tooling replaying EA results)."""
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; known: {STATUSES}")
        conn = self.connect()
        now = int(now_utc if now_utc is not None else time.time())
        with conn:
            cur = conn.execute(
                f"UPDATE {self.table} SET status = ?, updated_at_utc = ?, "
                f"comment = COALESCE(?, comment) WHERE intent_id = ?",
                (status, now, comment, intent_id))
        return cur.rowcount > 0

    def expire_stale(self, now_utc: int | None = None) -> int:
        """Flip expired-but-unconsumed rows to 'expired' (housekeeping)."""
        conn = self.connect()
        now = int(now_utc if now_utc is not None else time.time())
        with conn:
            cur = conn.execute(
                f"UPDATE {self.table} SET status = 'expired', "
                f"updated_at_utc = ? WHERE status = 'new' "
                f"AND expires_at_utc IS NOT NULL AND expires_at_utc <= ?",
                (now, now))
        return cur.rowcount or 0

    def schema_version(self) -> int:
        return SCHEMA_VERSION


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
