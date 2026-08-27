"""Optimized SQLite connection management and pooling (P2-9)."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from typing import Generator, Optional


def configure_sqlite_connection(conn: sqlite3.Connection, timeout_ms: int = 5000) -> sqlite3.Connection:
    """Apply performance and concurrency pragmas to SQLite connection."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class SQLiteConnectionPool:
    """Thread-local SQLite connection manager for concurrency without lock contention."""

    def __init__(self, db_path: str, timeout: float = 10.0):
        self.db_path = db_path
        self.timeout = timeout
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._local = threading.local()
        self._all_conns = []
        self._lock = threading.Lock()

    def get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=self.timeout)
            configure_sqlite_connection(conn)
            self._local.conn = conn
            with self._lock:
                self._all_conns.append(conn)
        return conn

    @contextlib.contextmanager
    def cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self.get_connection()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def close_all(self) -> None:
        with self._lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
            if hasattr(self._local, "conn"):
                self._local.conn = None
