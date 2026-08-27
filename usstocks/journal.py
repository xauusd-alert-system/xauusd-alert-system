"""SQLite journal for the usstocks signal-only subsystem (ТЗ §10).

Tables: us_sessions, us_watchlist_snapshots, us_signals, us_trade_outcomes,
us_risk_events. Every row keeps source/strategy-version so later analysis can
trust its provenance. CSV export is pandas-free (stdlib csv) and daily.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from usstocks.models import PremarketSnapshot, RiskEvent, TradeSignal

STRATEGY_VERSION = "vwap_pullback_continuation-v1"
SOURCE = "utex"

CURRENT_SCHEMA_VERSION = 2

_MIGRATION_DESCRIPTIONS = {
    1: "Initial tables (sessions, snapshots, signals, outcomes, risk_events)",
    2: "Performance indexes on signals, outcomes, snapshots, and risk_events",
}

_MIGRATIONS = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        description TEXT
    );
    CREATE TABLE IF NOT EXISTS us_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_date TEXT UNIQUE NOT NULL,
        opened_at TEXT NOT NULL,
        closed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS us_watchlist_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_date TEXT NOT NULL,
        ts TEXT NOT NULL,
        symbol TEXT NOT NULL,
        price REAL, prev_close REAL, gap_pct REAL, relative_volume REAL,
        avg_daily_dollar_volume REAL, spread_pct REAL, score INTEGER,
        in_watchlist INTEGER NOT NULL DEFAULT 0,
        strategy_version TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS us_signals (
        signal_id TEXT PRIMARY KEY,
        session_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        entry_low REAL, entry_high REAL, stop REAL, tp1 REAL, tp2 REAL,
        risk_per_share REAL, shares INTEGER, notional_usd REAL,
        planned_risk_usd REAL, grade TEXT,
        strategy_version TEXT NOT NULL,
        provider TEXT NOT NULL,
        metrics_json TEXT, passed_json TEXT, why_json TEXT,
        decision TEXT NOT NULL DEFAULT 'pending'   -- pending|accepted|rejected|taken
    );
    CREATE TABLE IF NOT EXISTS us_trade_outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT REFERENCES us_signals(signal_id),
        recorded_at TEXT NOT NULL,
        outcome TEXT NOT NULL,                      -- win|loss|flat|manual
        pnl_usd REAL NOT NULL,
        r_multiple REAL,
        confirmed_by TEXT NOT NULL,                 -- telegram chat id
        note TEXT
    );
    CREATE TABLE IF NOT EXISTS us_risk_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        session_date TEXT NOT NULL,
        symbol TEXT,
        code TEXT NOT NULL,
        allowed INTEGER NOT NULL,
        reason TEXT
    );
    """,
    2: """
    CREATE INDEX IF NOT EXISTS idx_signals_date ON us_signals(session_date);
    CREATE INDEX IF NOT EXISTS idx_signals_decision_created ON us_signals(decision, created_at);
    CREATE INDEX IF NOT EXISTS idx_signals_symbol_decision ON us_signals(symbol, decision, created_at);
    CREATE INDEX IF NOT EXISTS idx_outcomes_signal ON us_trade_outcomes(signal_id);
    CREATE INDEX IF NOT EXISTS idx_outcomes_recorded ON us_trade_outcomes(recorded_at);
    CREATE INDEX IF NOT EXISTS idx_watchlist_date_symbol ON us_watchlist_snapshots(session_date, symbol);
    CREATE INDEX IF NOT EXISTS idx_risk_events_date_ts ON us_risk_events(session_date, ts);
    """,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JournalExportError(Exception):
    """Raised when journal CSV export fails."""
    pass


class UsJournal:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._run_migrations()

    def _run_migrations(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT)"
        )
        self._conn.commit()

        applied = {
            r[0] for r in self._conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for ver in sorted(_MIGRATIONS):
            if ver not in applied:
                self._conn.executescript(_MIGRATIONS[ver])
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at, description) VALUES (?, ?, ?)",
                    (ver, _now_iso(), _MIGRATION_DESCRIPTIONS.get(ver, "")),
                )
                self._conn.commit()

    def get_schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"] or 0) if row else 0

    # -- sessions ----------------------------------------------------------

    def ensure_session(self, session_date: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO us_sessions(session_date, opened_at) "
            "VALUES (?, ?)", (session_date, _now_iso()))
        self._conn.commit()

    def close_session(self, session_date: str) -> None:
        self._conn.execute(
            "UPDATE us_sessions SET closed_at=? WHERE session_date=?",
            (_now_iso(), session_date))
        self._conn.commit()

    # -- watchlist ---------------------------------------------------------

    def save_watchlist(self, session_date: str, snapshots, in_watchlist) -> None:
        """snapshots: PremarketSnapshot list; in_watchlist: set of symbols."""
        now = _now_iso()
        rows = [(session_date, now, s.symbol, s.price, s.prev_close, s.gap_pct,
                 s.relative_volume, s.avg_daily_dollar_volume, s.spread_pct,
                 s.score, 1 if s.symbol.upper() in {w.upper() for w in in_watchlist} else 0,
                 STRATEGY_VERSION) for s in snapshots]
        self._conn.executemany(
            "INSERT INTO us_watchlist_snapshots(session_date, ts, symbol, price,"
            " prev_close, gap_pct, relative_volume, avg_daily_dollar_volume,"
            " spread_pct, score, in_watchlist, strategy_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self._conn.commit()

    # -- signals -----------------------------------------------------------

    def save_signal(self, sig: TradeSignal, session_date: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO us_signals(signal_id, session_date, created_at,"
            " symbol, side, entry_low, entry_high, stop, tp1, tp2, risk_per_share,"
            " shares, notional_usd, planned_risk_usd, grade, strategy_version,"
            " provider, metrics_json, passed_json, why_json, decision)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
            (sig.signal_id, session_date, sig.created_at.isoformat(),
             sig.symbol, sig.side, sig.entry_low, sig.entry_high, sig.stop,
             sig.tp1, sig.tp2, sig.risk_per_share, sig.shares, sig.notional_usd,
             sig.planned_risk_usd, sig.grade, sig.strategy_version, SOURCE,
             json.dumps(sig.metrics), json.dumps(sig.passed_checks),
             json.dumps(sig.why)))
        self._conn.commit()

    def mark_decision(self, signal_id: str, decision: str) -> None:
        self._conn.execute(
            "UPDATE us_signals SET decision=? WHERE signal_id=?",
            (decision, signal_id))
        self._conn.commit()

    def latest_signal(self, symbol: Optional[str] = None,
                      decision: str = "pending") -> Optional[sqlite3.Row]:
        q = ("SELECT * FROM us_signals WHERE decision=?"
             + (" AND symbol=?" if symbol else "")
             + " ORDER BY created_at DESC LIMIT 1")
        args = (decision, symbol) if symbol else (decision,)
        row = self._conn.execute(q, args).fetchone()
        return row

    # -- outcomes ----------------------------------------------------------

    def record_outcome(self, signal_id: str, *, pnl_usd: float,
                       planned_risk_usd: float, confirmed_by: str,
                       outcome: Optional[str] = None,
                       note: str = "") -> int:
        if outcome is None:
            outcome = ("win" if pnl_usd > 0 else
                       "loss" if pnl_usd < 0 else "flat")
        r_multiple = (pnl_usd / planned_risk_usd) if planned_risk_usd > 0 else None
        cur = self._conn.execute(
            "INSERT INTO us_trade_outcomes(signal_id, recorded_at, outcome,"
            " pnl_usd, r_multiple, confirmed_by, note) VALUES (?,?,?,?,?,?,?)",
            (signal_id, _now_iso(), outcome, round(pnl_usd, 2),
             None if r_multiple is None else round(r_multiple, 3),
             confirmed_by, note))
        self.mark_decision(signal_id, "taken")
        self._conn.commit()
        return cur.lastrowid

    def day_pnl(self, session_date: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM us_trade_outcomes o"
            " JOIN us_signals g ON g.signal_id=o.signal_id"
            " WHERE g.session_date=?", (session_date,)).fetchone()
        return float(row["s"] or 0.0)

    # -- risk events -------------------------------------------------------

    def save_risk_event(self, event: RiskEvent, session_date: str) -> None:
        self._conn.execute(
            "INSERT INTO us_risk_events(ts, session_date, symbol, code, allowed,"
            " reason) VALUES (?,?,?,?,?,?)",
            (_now_iso(), session_date, event.symbol, event.code,
             1 if event.allowed else 0, event.reason))
        self._conn.commit()

    # -- export ------------------------------------------------------------

    def export_day_csv(self, session_date: str, out_dir: str) -> str:
        if not session_date or not isinstance(session_date, str):
            raise JournalExportError(f"Invalid session_date for export: {session_date!r}")
        try:
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"us_signals_{session_date}.csv")
            tmp_path = path + f".tmp.{os.getpid()}"
            rows = self._conn.execute(
                "SELECT g.*, o.outcome, o.pnl_usd, o.r_multiple, o.confirmed_by"
                " FROM us_signals g LEFT JOIN us_trade_outcomes o"
                " ON o.signal_id=g.signal_id WHERE g.session_date=?"
                " ORDER BY g.created_at", (session_date,)).fetchall()
            if rows:
                cols = list(rows[0].keys())
            else:
                cols = ["signal_id", "session_date"]
            with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for r in rows:
                    w.writerow([r[c] for c in cols])
            os.replace(tmp_path, path)
            return path
        except Exception as e:
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if isinstance(e, JournalExportError):
                raise
            raise JournalExportError(f"Failed to export CSV for {session_date}: {e}") from e

    def close(self) -> None:
        self._conn.close()
