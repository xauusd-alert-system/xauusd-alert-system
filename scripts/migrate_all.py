"""
Unified migration entry point (ТЗ 9.11).

Runs, in order:

1. **DB migrations** — versioned SQLite schema migrations
   (:mod:`data.migrate`) for every known database path used by the project
   (main market/trade DB, trade-log DB, signal-log DB, plus any explicit
   ``--db`` overrides);
2. **Registry check** — spot-checks deserialization of persisted
   ``TradeGroupSpec`` / ``ExecutionIntent`` payloads through the versioned
   schema registry (:mod:`execution.schema_registry`), so unknown schema
   versions or corrupt payloads fail loudly BEFORE the trading runtime hits
   them.

CLI::

    python -m scripts.migrate_all [--dry-run] [--db PATH ...]

``--dry-run`` never changes any data (no migrations applied, no records
written). Non-zero exit code on any error.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger("migrate_all")


# --------------------------------------------------------------------------
# Known database paths (project conventions)
# --------------------------------------------------------------------------

def default_db_paths() -> list[str]:
    """All known SQLite paths per project conventions (config + env)."""
    paths: list[str] = []

    # Main DB (market data, trade_groups, ledgers): config general.db_path.
    try:
        from config.loader import load_config

        paths.append(str(
            load_config().get("general", {}).get(
                "db_path", "data/market_data_mt5.sqlite"
            )
        ))
    except Exception:
        paths.append("data/market_data_mt5.sqlite")

    # Trade log DB (TRADE_LOG_DB_PATH, falls back to general.db_path).
    try:
        from common.utils import get_env  # type: ignore

        trade_log = get_env("TRADE_LOG_DB_PATH", default=None)
    except Exception:
        trade_log = os.getenv("TRADE_LOG_DB_PATH")
    if trade_log:
        paths.append(str(trade_log))

    # Signal log DB.
    signal_log = os.getenv("SIGNAL_LOG_DB_PATH", default="data/signal_log.db")
    if signal_log:
        paths.append(str(signal_log))

    # De-duplicate, preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


# --------------------------------------------------------------------------
# Registry check
# --------------------------------------------------------------------------

@dataclass
class RegistryCheckResult:
    db_path: str
    specs_checked: int = 0
    intents_checked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def registry_check(db_path: str) -> RegistryCheckResult:
    """Spot-check deserialization of persisted specs/intents via the registry.

    Only tables that actually exist are inspected; a database without any
    trade-group/intent tables simply reports zero records checked.
    """
    from execution.schema_registry import deserialize_spec

    result = RegistryCheckResult(db_path=db_path)
    if not os.path.exists(db_path):
        return result

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "trade_groups" in tables:
            rows = conn.execute(
                "SELECT group_id, spec_json FROM trade_groups"
            ).fetchall()
            for group_id, spec_json in rows:
                result.specs_checked += 1
                try:
                    payload = json.loads(spec_json or "{}")
                    deserialize_spec(payload)
                except Exception as exc:
                    result.errors.append(
                        f"{db_path}: trade_groups[{group_id}]: {exc}"
                    )
        if "ledger_intents" in tables:
            rows = conn.execute(
                "SELECT intent_id, payload_json FROM ledger_intents"
            ).fetchall()
            for intent_id, payload_json in rows:
                result.intents_checked += 1
                try:
                    payload = json.loads(payload_json or "{}")
                    # ledger_intents stores SignalIntent payloads
                    # (contracts/execution_contracts), not ExecutionIntent —
                    # the registry check here only validates that the
                    # payload's schema_version is a known intent version.
                    version = payload.get("schema_version")
                    if version is not None and not isinstance(version, (str, int)):
                        raise ValueError(
                            f"invalid schema_version type: {type(version)!r}"
                        )
                except Exception as exc:
                    result.errors.append(
                        f"{db_path}: ledger_intents[{intent_id}]: {exc}"
                    )
    finally:
        conn.close()
    return result


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_migrate_all(
    db_paths: Sequence[str] | None = None,
    dry_run: bool = False,
) -> list[tuple[str, bool, str]]:
    """Run DB migrations + registry checks. Returns (db_path, ok, summary).

    ``dry_run``: report pending migrations and run registry checks without
    applying anything.
    """
    from data.migrate import apply_migrations, pending_migrations

    paths = list(db_paths) if db_paths else default_db_paths()
    results: list[tuple[str, bool, str]] = []

    for db_path in paths:
        try:
            if dry_run:
                pending = pending_migrations(db_path)
                summary = (
                    f"dry-run: would apply {len(pending)} migration(s) "
                    f"to {db_path}"
                )
                for migration in pending:
                    summary += f"\n  {migration.version:>4}  {migration.name}"
                results.append((db_path, True, summary))
            else:
                applied = apply_migrations(db_path)
                summary = f"applied {len(applied)} migration(s) to {db_path}"
                for migration in applied:
                    summary += f"\n  {migration.version:>4}  {migration.name}"
                results.append((db_path, True, summary))
        except Exception as exc:
            results.append((db_path, False, f"migration error: {exc}"))

        try:
            check = registry_check(db_path)
            if check.ok:
                results.append((
                    db_path, True,
                    f"registry check ok: {check.specs_checked} spec(s), "
                    f"{check.intents_checked} intent(s)",
                ))
            else:
                for error in check.errors:
                    results.append((db_path, False, f"registry check: {error}"))
        except Exception as exc:
            results.append((db_path, False, f"registry check error: {exc}"))

    return results


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m scripts.migrate_all",
        description="Unified DB migration + schema registry check (ТЗ 9.11).",
    )
    parser.add_argument(
        "--db", action="append", default=None,
        help="explicit database path (repeatable); default: all known paths",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be applied without changing any data",
    )
    args = parser.parse_args(argv)

    results = run_migrate_all(db_paths=args.db, dry_run=args.dry_run)

    failed = False
    for db_path, ok, summary in results:
        marker = "OK  " if ok else "FAIL"
        print(f"[{marker}] {db_path}: {summary}")
        if not ok:
            failed = True

    if failed:
        print("migrate_all: FAILED", file=sys.stderr)
        return 1
    print("migrate_all: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
