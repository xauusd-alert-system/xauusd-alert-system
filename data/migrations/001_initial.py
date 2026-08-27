"""Migration 001 — ``initial``: verification of the existing schema (ТЗ §9.3).

The project creates its tables lazily via ``CREATE TABLE IF NOT EXISTS``
inside the ``data/*.py`` store modules (data/storage.py, data/signal_log.py,
data/trade_group_store.py, data/intent_ledger.py, data/ledger_events.py,
data/ledger_bridge.py, data/trading_event_ledger.py, data/execution_ledger.py,
data/trade_logger.py). This migration therefore deliberately does NOT create
any tables — that would duplicate the store initializers and risk drift.

What it does verify:

* on a fresh/empty database (or a foreign database with none of the known
  application tables) it is a pure no-op;
* when a table family is partially initialized (e.g. ``trade_groups`` exists
  but its companion ``trade_group_actions`` is missing) the schema is broken
  and the migration fails loudly instead of letting the runtime hit cryptic
  "no such column" errors later.

This keeps the migration idempotent and backwards compatible with every
database the project already uses (candles-only DBs, signal-log-only DBs,
combined trade DBs).
"""

VERSION = 1
NAME = "initial"

# Tables known to be created by the data layer. Presence is informational —
# databases legitimately differ in which subsystems they host.
KNOWN_TABLES = (
    "ohlcv_m1", "ohlcv_m5", "ohlcv_m15", "ohlcv_h1", "ohlcv_h4",
    "signal_log",
    "trade_groups", "trade_group_actions",
    "ledger_intents", "ledger_events", "ledger_outbox",
    "trading_events", "execution_fills", "executed_trades",
    "channel_archive_messages",
)

# Families that MUST always be created together by one initializer. A
# partially initialized family means the schema is broken.
TABLE_FAMILIES: dict[str, tuple[str, ...]] = {
    "trade_groups": ("trade_groups", "trade_group_actions"),
}


def apply(conn) -> None:
    existing = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    present = [table for table in KNOWN_TABLES if table in existing]
    if not present:
        # Fresh or non-application database: nothing to verify yet (no-op).
        return
    for family_name, tables in TABLE_FAMILIES.items():
        found = [table for table in tables if table in existing]
        if found and len(found) != len(tables):
            missing = [table for table in tables if table not in existing]
            raise RuntimeError(
                f"migration {VERSION} ({NAME}): partially initialized table "
                f"family {family_name!r}: missing {missing}; the schema was "
                f"not created by the current data layer initializers"
            )
