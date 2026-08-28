"""SQLite schema migration runner (ТЗ §9.3).

Tracks applied schema versions in a dedicated ``schema_migrations`` table:

    schema_migrations(
        version         INTEGER PRIMARY KEY,
        name            TEXT NOT NULL,
        applied_at_utc_ms INTEGER NOT NULL
    )

Guarantees:

* every migration runs inside a ``BEGIN IMMEDIATE`` transaction — either the
  whole migration is applied and recorded, or nothing changes;
* applying is idempotent: already-recorded versions are skipped;
* migrations are recorded AFTER their transaction commits successfully;
* a failing migration rolls back its own changes and never leaves a recorded
  version behind.

CLI::

    python -m data.migrate [--db PATH] [--dry-run] [--status]
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from data.storage import get_connection

MIGRATIONS_TABLE = "schema_migrations"

MIGRATIONS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at_utc_ms INTEGER NOT NULL
)
"""


@dataclass(frozen=True)
class Migration:
    """One versioned schema migration."""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(MIGRATIONS_TABLE_SQL)
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute(f"SELECT version, name FROM {MIGRATIONS_TABLE} ORDER BY version").fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


def load_builtin_migrations() -> list[Migration]:
    """Discover migrations in :mod:`data.migrations` ordered by ``VERSION``.

    Every public submodule must expose ``VERSION``, ``NAME`` and ``apply``.
    Duplicate versions raise immediately — they are always a bug.
    """
    import data.migrations as package

    migrations: list[Migration] = []
    seen: dict[int, str] = {}
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"data.migrations.{module_info.name}")
        version = int(module.VERSION)
        name = str(module.NAME)
        if version in seen:
            raise RuntimeError(f"duplicate migration version {version}: {seen[version]!r} and {module_info.name!r}")
        seen[version] = module_info.name
        migrations.append(Migration(version=version, name=name, apply=module.apply))
    return migrations


def current_version(db_path: str) -> int:
    """Highest applied migration version; 0 for a fresh database."""
    conn = get_connection(db_path)
    try:
        _ensure_migrations_table(conn)
        applied = _applied_versions(conn)
    finally:
        conn.close()
    return max(applied, default=0)


def pending_migrations(
    db_path: str,
    migrations: Sequence[Migration] | None = None,
) -> list[Migration]:
    """Migrations not yet recorded for ``db_path``, ordered by version."""
    if migrations is None:
        migrations = load_builtin_migrations()
    conn = get_connection(db_path)
    try:
        _ensure_migrations_table(conn)
        applied = _applied_versions(conn)
    finally:
        conn.close()
    return [m for m in migrations if m.version not in applied]


def apply_migrations(
    db_path: str,
    migrations: Sequence[Migration] | None = None,
    dry_run: bool = False,
) -> list[Migration]:
    """Apply all pending migrations; returns the migrations actually applied.

    ``dry_run=True`` reports what WOULD be applied without touching the
    database (beyond creating the bookkeeping table on a fresh DB — no
    application schema is changed).

    Each migration runs in its own ``BEGIN IMMEDIATE`` transaction; the
    ``schema_migrations`` record is written inside the SAME transaction, so a
    crash can never leave a migration applied-but-unrecorded or the reverse.
    """
    if migrations is None:
        migrations = load_builtin_migrations()
    pending = pending_migrations(db_path, migrations)
    if dry_run or not pending:
        return [] if dry_run else list(pending)

    applied: list[Migration] = []
    conn = get_connection(db_path)
    try:
        _ensure_migrations_table(conn)
        already = _applied_versions(conn)
        for migration in sorted(pending, key=lambda m: m.version):
            if migration.version in already:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                migration.apply(conn)
                conn.execute(
                    f"INSERT INTO {MIGRATIONS_TABLE} (version, name, applied_at_utc_ms) VALUES (?, ?, ?)",
                    (migration.version, migration.name, _now_ms()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            applied.append(migration)
    finally:
        conn.close()
    return applied


def _format_status(db_path: str) -> str:
    migrations = load_builtin_migrations()
    conn = get_connection(db_path)
    try:
        _ensure_migrations_table(conn)
        applied = _applied_versions(conn)
    finally:
        conn.close()
    pending = [m for m in migrations if m.version not in applied]
    lines = [f"Database: {db_path}"]
    lines.append(f"Applied ({len(applied)}):")
    for version in sorted(applied):
        lines.append(f"  {version:>4}  {applied[version]}")
    if not applied:
        lines.append("  (none)")
    lines.append(f"Pending ({len(pending)}):")
    for migration in pending:
        lines.append(f"  {migration.version:>4}  {migration.name}")
    if not pending:
        lines.append("  (none)")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data.migrate",
        description="Apply versioned SQLite schema migrations (ТЗ §9.3).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="database path (default: config general.db_path or data/market_data_mt5.sqlite)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be applied without changing the database",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print applied and pending migrations, then exit",
    )
    args = parser.parse_args(argv)

    if args.db:
        db_path: str = args.db
    else:
        try:
            from config.loader import load_config

            db_path = str(load_config().get("general", {}).get("db_path", "data/market_data_mt5.sqlite"))
        except Exception:
            db_path = "data/market_data_mt5.sqlite"

    if args.status:
        print(_format_status(db_path))
        return 0

    if args.dry_run:
        pending = pending_migrations(db_path)
        print(f"Would apply {len(pending)} migrations to {db_path}")
        for migration in pending:
            print(f"  {migration.version:>4}  {migration.name}")
        return 0

    applied = apply_migrations(db_path)
    print(f"Applied {len(applied)} migrations to {db_path}")
    for migration in applied:
        print(f"  {migration.version:>4}  {migration.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
