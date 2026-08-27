"""ТЗ 6.3: component health checks for the enriched ``/api/health`` endpoint.

Each component is a callable returning ``(ok: bool, detail: str)`` and is
aggregated by ``services.base.run_checks`` (the same contract the standalone
services already use). A check that raises is reported as a failed check by
``run_checks`` — a health probe must never 500.

Components (ТЗ 6.3 + Часть 6 criteria):

    db        — main SQLite database opens and (optionally) migrations applied;
    feed      — tick freshness for enabled assets (skipped gracefully when the
                MT5 terminal is unavailable — "если есть данные");
    executor  — number of active (non-terminal) trade groups;
    risk      — circuit-breaker flag from the persisted risk state;
    services  — configured service health ports (no network calls by default).

Degraded (status != ok), never an HTTP error, per the ТЗ.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Callable

logger = logging.getLogger("monitoring.health")

# Group states that still require (or may soon require) executor management.
# Anything else is terminal and does not count as "active".
TERMINAL_GROUP_STATES = {
    "DRAFT", "RECONCILED", "STOPPED", "REJECTED", "EXPIRED",
    "CANCELLED", "FAILED",
}

DEFAULT_RISK_STATE_PATH = "logs/risk_state.json"
DEFAULT_FEED_STALENESS_S = 30.0


def db_check(db_path: str) -> Callable[[], "tuple[bool, str]"]:
    """Main DB reachable; report whether migrations have been applied."""

    def _check() -> "tuple[bool, str]":
        if not os.path.exists(db_path):
            return False, f"database file missing: {db_path}"
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        if "schema_migrations" in tables:
            return True, "db ok (migrations applied)"
        return True, "db ok (no migration table)"

    return _check


def executor_check(db_path: str) -> Callable[[], "tuple[bool, str]"]:
    """Count active trade groups via a lightweight direct SQL read.

    The check is observability-only and intentionally fail-open: if the
    trade-group store does not exist yet (fresh deployment) the component is
    reported as ok with an explanatory detail instead of degrading health.
    """

    def _check() -> "tuple[bool, str]":
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = conn.execute(
                    "SELECT state, COUNT(*) FROM trade_groups GROUP BY state"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return True, "trade-group store not initialised (skipped)"
        counts = {str(state): int(n) for state, n in rows}
        active = sum(n for state, n in counts.items()
                     if state not in TERMINAL_GROUP_STATES)
        detail = f"{active} active groups"
        if counts:
            detail += f" ({json.dumps(counts, sort_keys=True)})"
        return True, detail

    return _check


def risk_check(state_path: str = DEFAULT_RISK_STATE_PATH) -> Callable[[], "tuple[bool, str]"]:
    """Circuit breaker flag from the persisted risk state (risk/state.py)."""

    def _check() -> "tuple[bool, str]":
        if not os.path.exists(state_path):
            return True, "no persisted risk state"
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"risk state unreadable: {exc}"
        if data.get("circuit_breaker_tripped"):
            return False, "circuit breaker tripped"
        return True, "circuit breaker clear"

    return _check


def feed_check(
    symbols: list[str] | None = None,
    staleness_s: float = DEFAULT_FEED_STALENESS_S,
    clock=time.time,
) -> Callable[[], "tuple[bool, str]"]:
    """Tick freshness for enabled assets (ТЗ 6.19 semantics, minimal form).

    Fail-open design: when the MT5 terminal is not connected (paper mode,
    unit tests, weekends) the check reports ok with a "skipped" detail —
    absence of a feed source is not a health incident for the API process.
    A tick that EXISTS but is older than ``staleness_s`` degrades health.
    """

    def _check() -> "tuple[bool, str]":
        try:
            from alerts import status_commands as sc

            if not sc.ensure_mt5_connection():
                return True, "mt5 not connected (feed check skipped)"
            mt5_mod = sc.get_mt5()
            checked: list[str] = []
            for symbol in symbols or []:
                tick = mt5_mod.symbol_info_tick(symbol)
                if tick is None or not getattr(tick, "time", 0):
                    continue
                age = clock() - float(tick.time)
                checked.append(f"{symbol}:{age:.0f}s")
                if age > staleness_s:
                    return False, f"feed stale: {symbol} tick age {age:.0f}s"
            if not checked:
                return True, "no tick data available (skipped)"
            return True, "feed fresh (" + ", ".join(checked) + ")"
        except Exception as exc:  # noqa: BLE001 — probe must never raise
            return True, f"feed check skipped: {exc}"

    return _check


def services_check(cfg: dict) -> Callable[[], "tuple[bool, str]"]:
    """Configured service health ports — static report, NO network calls."""

    def _check() -> "tuple[bool, str]":
        services = (cfg or {}).get("services", {}) or {}
        ports = {
            str(name): svc.get("health_port")
            for name, svc in sorted(services.items())
            if isinstance(svc, dict) and svc.get("health_port")
        }
        if not ports:
            return True, "no services configured"
        return True, "configured: " + ", ".join(
            f"{name}:{port}" for name, port in ports.items()
        )

    return _check


def build_health_checks(cfg: dict, db_path: str | None = None) -> dict:
    """Build the named-check mapping consumed by services.base.run_checks."""
    cfg = cfg or {}
    resolved_db = db_path or cfg.get("general", {}).get(
        "db_path", "data/market_data_mt5.sqlite"
    )
    assets = cfg.get("assets", {}) or {}
    symbols = [
        str(a.get("mt5_symbol") or key)
        for key, a in assets.items()
        if isinstance(a, dict) and a.get("enabled")
    ]
    return {
        "db": db_check(resolved_db),
        "executor": executor_check(resolved_db),
        "risk": risk_check(),
        "feed": feed_check(symbols=symbols or None),
        "services": services_check(cfg),
    }
