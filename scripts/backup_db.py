"""ТЗ 6.5: SQLite backup script with retention; ТЗ 6.10: restore.

Backup: creates a consistent backup of the main SQLite database (via the
sqlite3 online-backup API — safe against concurrent writers, unlike a raw
file copy) plus the persisted risk-state file into ``backups/``, then prunes
old backups keeping the N most recent (config ``monitoring.backup.keep``,
default 7).

    python -m scripts.backup_db [--db-path PATH] [--backup-dir DIR] [--dry-run]

Restore (ТЗ 6.10 disaster recovery): replaces the live DB with a backup
after an integrity check. Destructive — requires explicit confirmation:
``--yes`` (non-interactive, e.g. runbooks) or an interactive prompt when
stdin is a TTY. Refuses (exit 2) when neither is available, so a stray
``--restore`` flag in a script can never silently wipe the database.

    python -m scripts.backup_db --restore backups/market_data_mt5.sqlite.bak [--yes]

Backups are local-only artifacts (``backups/`` is git-ignored) and are never
committed. Full procedure: docs/RECOVERY.md.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config  # noqa: E402

logger = logging.getLogger("backup_db")

DEFAULT_BACKUP_DIR = "backups"
DEFAULT_KEEP = 7
RISK_STATE_PATH = "logs/risk_state.json"


def backup_database(
    db_path: str,
    backup_dir: str,
    keep: int = DEFAULT_KEEP,
    dry_run: bool = False,
    risk_state_path: str | None = RISK_STATE_PATH,
) -> list[str]:
    """Back up the DB (+risk state) and prune old backups.

    Returns the list of created backup file paths. ``dry_run`` performs the
    full plan but writes/deletes nothing.
    """
    keep = max(1, int(keep))
    os.makedirs(backup_dir, exist_ok=True) if not dry_run else None
    created: list[str] = []

    if os.path.exists(db_path):
        target = os.path.join(backup_dir, os.path.basename(db_path) + ".bak")
        if dry_run:
            logger.info("[dry-run] would back up %s -> %s", db_path, target)
        else:
            # sqlite3 online backup API: consistent even while writers run.
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(target)
                try:
                    src.backup(dst)
                    dst.commit()
                finally:
                    dst.close()
            finally:
                src.close()
            created.append(target)
            logger.info("backed up %s -> %s", db_path, target)
    else:
        logger.warning("db_path %s does not exist — skipping DB backup", db_path)

    if risk_state_path and os.path.exists(risk_state_path):
        target = os.path.join(
            backup_dir, os.path.basename(risk_state_path) + ".bak")
        if dry_run:
            logger.info("[dry-run] would copy %s -> %s", risk_state_path, target)
        else:
            with open(risk_state_path, "r", encoding="utf-8") as f:
                data = f.read()
            with open(target, "w", encoding="utf-8") as f:
                f.write(data)
            created.append(target)
            logger.info("backed up %s -> %s", risk_state_path, target)

    pruned = prune_backups(backup_dir, keep=keep, dry_run=dry_run)
    if pruned:
        logger.info("%s %d old backup file(s)",
                    "[dry-run] would remove" if dry_run else "removed", len(pruned))
    return created


def prune_backups(backup_dir: str, keep: int, dry_run: bool = False) -> list[str]:
    """Keep the N most recent ``*.bak`` files (by mtime), remove the rest."""
    if not os.path.isdir(backup_dir):
        return []
    candidates = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.endswith(".bak")
    ]
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    stale = candidates[keep:]
    removed: list[str] = []
    for path in stale:
        if dry_run:
            logger.info("[dry-run] would remove %s", path)
        else:
            os.remove(path)
            logger.info("removed old backup %s", path)
        removed.append(path)
    return removed


def validate_backup(backup_path: str) -> bool:
    """Integrity check: backup opens and answers a trivial query."""
    try:
        conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA integrity_check").fetchone()
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True
    except sqlite3.Error as exc:
        logger.error("backup %s invalid: %s", backup_path, exc)
        return False


def restore_database(backup_path: str, db_path: str,
                     risk_state_path: str | None = None) -> str:
    """ТЗ 6.10: replace the live DB with a backup.

    Fails fast (``FileNotFoundError`` / ``ValueError``) when the backup does
    not exist or fails ``PRAGMA integrity_check``. The live DB is pre-copied
    to ``<db>.pre_restore.bak`` (git-ignored) so the restore itself is
    reversible; stale ``-wal``/``-shm`` sidecars of the replaced DB are
    removed so SQLite cannot mix pages across versions. Returns the
    pre-restore safety-copy path ("" when the live DB did not exist).
    """
    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"backup not found: {backup_path}")
    if not validate_backup(backup_path):
        raise ValueError(
            f"backup {backup_path} failed integrity_check — refusing restore")

    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)

    safety_copy = ""
    if os.path.exists(db_path):
        safety_copy = db_path + ".pre_restore.bak"
        shutil.copy2(db_path, safety_copy)
        logger.info("pre-restore safety copy: %s -> %s", db_path, safety_copy)
        for sidecar in (db_path + "-wal", db_path + "-shm",
                        db_path + "-journal"):
            if os.path.exists(sidecar):
                os.remove(sidecar)
                logger.info("removed stale sidecar %s", sidecar)

    # Replace via a temp sibling + os.replace so readers never see a partial file.
    tmp_path = db_path + ".restore.tmp"
    shutil.copy2(backup_path, tmp_path)
    os.replace(tmp_path, db_path)
    logger.info("restored %s -> %s", backup_path, db_path)

    if risk_state_path and os.path.exists(risk_state_path + ".bak"):
        shutil.copy2(risk_state_path + ".bak", risk_state_path)
        logger.info("restored risk state %s", risk_state_path)

    return safety_copy


def _confirm_restore(backup_path: str, db_path: str) -> bool:
    """Interactive confirmation; refuses when stdin is not a TTY (ТЗ 6.10).

    A non-interactive ``--restore`` without ``--yes`` must fail closed: there
    is no operator to answer the prompt, so silently proceeding would make
    the flag destructive-by-accident in scripts/CI.
    """
    if not sys.stdin.isatty():
        logger.error(
            "refusing restore: no --yes and stdin is not a TTY "
            "(non-interactive context). Pass --yes to confirm explicitly.")
        return False
    answer = input(
        f"Replace {db_path} with {backup_path}? Type RESTORE to confirm: ")
    if answer.strip() != "RESTORE":
        logger.error("confirmation mismatch — restore aborted")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up the main SQLite DB (+risk state) with retention; "
                    "optionally restore a backup (ТЗ 6.10).")
    parser.add_argument("--db-path", default=None,
                        help="main SQLite DB (default: config general.db_path)")
    parser.add_argument("--backup-dir", default=None,
                        help="backup destination (default: backups/)")
    parser.add_argument("--keep", type=int, default=None,
                        help="number of recent backups to retain "
                             "(default: config monitoring.backup.keep, else 7)")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only: create nothing, delete nothing")
    parser.add_argument("--restore", default=None, metavar="BACKUP",
                        help="ТЗ 6.10: replace the live DB with BACKUP "
                             "(integrity-checked; destructive)")
    parser.add_argument("--yes", action="store_true",
                        help="with --restore: skip the interactive confirmation")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = load_config()
    db_path = args.db_path or cfg.get("general", {}).get(
        "db_path", "data/market_data_mt5.sqlite")

    if args.restore:
        if not args.yes and not _confirm_restore(args.restore, db_path):
            return 2
        try:
            restore_database(args.restore, db_path,
                             risk_state_path=RISK_STATE_PATH)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("restore failed: %s", exc)
            return 1
        logger.info("restore complete — run 'python -m scripts.migrate_all "
                    "--dry-run' before restarting (docs/RECOVERY.md)")
        return 0

    backup_dir = args.backup_dir or (
        (cfg.get("monitoring", {}).get("backup", {}) or {}).get("dir")
        or DEFAULT_BACKUP_DIR
    )
    keep = args.keep
    if keep is None:
        keep = int((cfg.get("monitoring", {}).get("backup", {}) or {})
                   .get("keep", DEFAULT_KEEP))

    created = backup_database(db_path, backup_dir, keep=keep,
                              dry_run=args.dry_run)
    ok = True
    for path in created:
        if not validate_backup(path):
            ok = False
    logger.info("done: %d created, keep=%d, dry_run=%s",
                len(created), keep, args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
