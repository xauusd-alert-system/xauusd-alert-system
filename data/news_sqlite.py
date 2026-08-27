"""Historical news events in SQLite for backtests (task T-15).

The live news guard (``data/news_filter.py``) uses ForexFactory, and the
MQL5 economic-calendar API (book ch. 7.3) is FORBIDDEN in the Strategy
Tester (error 4014 FUNCTION_NOT_ALLOWED) - so a backtest that must model
news needs its OWN event table (book p. 1690). This module is that table:

* ``NewsStore`` - SQLite-backed event store (idempotent upsert by
  (timestamp, title, country));
* ``is_news_blackout`` - the +-N minute window check around high-impact
  events (mirrors the live guard's 30/30 buffers);
* CSV import for the documented dated format
  ``timestamp_utc,title,country,impact`` (the same format as the existing
  ``ensemble.historical_news_calendar_path`` option, now SQLite-backed).
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    country       TEXT    NOT NULL,
    impact        TEXT    NOT NULL DEFAULT 'high',
    created_at    INTEGER NOT NULL,
    UNIQUE (timestamp_utc, title, country)
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news_events (timestamp_utc);
"""


@dataclass
class NewsEvent:
    timestamp_utc: int
    title: str
    country: str
    impact: str = "high"


class NewsStore:
    """SQLite store of historical news events (backtest-side, task T-15)."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
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

    def __enter__(self) -> "NewsStore":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ io
    def upsert_events(self, events: list[NewsEvent]) -> int:
        """Idempotent insert (INSERT OR IGNORE on the natural key)."""
        conn = self.connect()
        now = int(datetime.now(tz=timezone.utc).timestamp())
        rows = [(int(e.timestamp_utc), e.title, e.country, e.impact, now)
                for e in events]
        with conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO news_events "
                "(timestamp_utc, title, country, impact, created_at) "
                "VALUES (?, ?, ?, ?, ?)", rows)
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def count(self) -> int:
        conn = self.connect()
        return int(conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0])

    def events_between(self, start_utc: int, end_utc: int,
                       countries: list[str] | None = None,
                       impact_min: str = "medium") -> list[NewsEvent]:
        """Events in [start, end] filtered by country (default: all)."""
        order = {"low": 0, "medium": 1, "high": 2}
        min_level = order.get(str(impact_min).lower(), 2)
        conn = self.connect()
        sql = ("SELECT timestamp_utc, title, country, impact FROM news_events "
               "WHERE timestamp_utc >= ? AND timestamp_utc <= ?")
        params: list = [int(start_utc), int(end_utc)]
        if countries:
            sql += " AND country IN (%s)" % ",".join("?" * len(countries))
            params += [c.upper() for c in countries]
        rows = conn.execute(sql, params).fetchall()
        out = []
        for ts, title, country, impact in rows:
            if order.get(str(impact).lower(), 0) >= min_level:
                out.append(NewsEvent(int(ts), title, country, impact))
        out.sort(key=lambda e: e.timestamp_utc)
        return out

    def import_csv(self, path: str | Path) -> int:
        """Import the dated CSV format: timestamp_utc,title,country,impact.

        ``timestamp_utc`` accepts epoch seconds or ``YYYY-MM-DD HH:MM`` /
        ISO-8601 strings (assumed UTC).
        """
        events = []
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                ts = _parse_ts(row.get("timestamp_utc") or row.get("timestamp"))
                if ts is None:
                    continue
                events.append(NewsEvent(
                    timestamp_utc=ts,
                    title=(row.get("title") or "").strip(),
                    country=(row.get("country") or "").strip().upper(),
                    impact=(row.get("impact") or "high").strip().lower(),
                ))
        return self.upsert_events(events)


def _parse_ts(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, fmt)
                       .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def is_news_blackout(ts_utc: int | datetime, events: list[NewsEvent],
                     buffer_before_min: int = 30,
                     buffer_after_min: int = 30) -> bool:
    """True inside +-N minutes around any listed event (mirror of the live
    guard's 30/30 buffers; book: NFP/FOMC/CPI are the XAUUSD-critical set)."""
    if isinstance(ts_utc, datetime):
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        ts_utc = int(ts_utc.timestamp())
    before = int(buffer_before_min) * 60
    after = int(buffer_after_min) * 60
    for e in events:
        if e.timestamp_utc - before <= ts_utc <= e.timestamp_utc + after:
            return True
    return False


def blackout_windows(events: list[NewsEvent], buffer_before_min: int = 30,
                     buffer_after_min: int = 30) -> list[tuple[int, int]]:
    """Merged (start, end) blackout windows for fast range queries."""
    if not events:
        return []
    before = int(buffer_before_min) * 60
    after = int(buffer_after_min) * 60
    spans = sorted((e.timestamp_utc - before, e.timestamp_utc + after)
                   for e in events)
    merged = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]
