"""ТЗ 6.2: alert manager with rules and cooldowns.

``AlertRule`` = (rule_name, condition callable, cooldown_sec, severity).
``AlertManager.evaluate(context)`` runs every rule against a context mapping;
when a condition fires AND the rule's cooldown has elapsed, the notifier
callback is invoked (TelegramAlertBot / a test mock — injected, never imported
hard). Repeat alerts of the same rule within ``cooldown_sec`` are suppressed.

Notifier contract: ``notifier(rule_name, severity, message)``; exceptions in
the notifier are swallowed and logged — an alert must never crash the caller.

Startup rules (built by ``default_rules``):
    FEED_STALE      — last tick older than N seconds (context["last_tick_age_s"]);
    CIRCUIT_BREAKER — risk circuit breaker tripped (context["circuit_breaker"]);
    DISK_LOW        — free disk below threshold_mb (uses monitoring/disk.py);
    MT5_DISCONNECT   — symbol_info_tick None N consecutive times
                      (context["mt5_disconnect_count"]).

Integration into run_bot is gated behind ``monitoring.alerts.enabled``
(default false) — no side effects unless explicitly enabled.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from monitoring.disk import disk_status

logger = logging.getLogger("monitoring.alerts")

Severity = str  # "P0" | "P1" | "P2"
Condition = Callable[[dict[str, Any]], bool]
Notifier = Callable[[str, str, str], None]


@dataclass(frozen=True)
class AlertRule:
    rule_name: str
    condition: Condition
    cooldown_sec: float
    severity: Severity = "P1"
    description: str = ""


@dataclass
class _RuleState:
    last_fired: float | None = None
    fire_count: int = 0
    suppressed: int = 0


class AlertManager:
    """Rule evaluation with per-rule cooldown suppression."""

    def __init__(
        self,
        rules: list[AlertRule],
        notifier: Notifier | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        if not rules:
            raise ValueError("AlertManager requires at least one rule")
        self.rules = {r.rule_name: r for r in rules}
        self.notifier = notifier
        self._clock = clock
        self._state: dict[str, _RuleState] = {
            name: _RuleState() for name in self.rules
        }

    def evaluate(self, context: dict[str, Any] | None = None) -> list[str]:
        """Run all rules; fire notifier for triggered, non-cooled-down rules.

        Returns the list of rule names that FIRED (notified) this pass.
        """
        context = context or {}
        fired: list[str] = []
        now = self._clock()
        for name, rule in self.rules.items():
            state = self._state[name]
            try:
                triggered = bool(rule.condition(context))
            except Exception as exc:  # noqa: BLE001 — a broken rule must not crash
                logger.warning("alert rule %s raised: %s", name, exc)
                continue
            if not triggered:
                continue
            if state.last_fired is not None and \
                    (now - state.last_fired) < rule.cooldown_sec:
                state.suppressed += 1
                logger.debug("alert %s suppressed by cooldown", name)
                continue
            state.last_fired = now
            state.fire_count += 1
            fired.append(name)
            if self.notifier is not None:
                message = rule.description or name
                try:
                    self.notifier(name, rule.severity, message)
                except Exception as exc:  # noqa: BLE001 — notifier failures logged
                    logger.warning("alert notifier failed for %s: %s", name, exc)
        return fired

    def rule_stats(self) -> dict[str, dict[str, Any]]:
        """Observability: fire/suppression counters per rule."""
        return {
            name: {"fires": st.fire_count, "suppressed": st.suppressed,
                   "last_fired": st.last_fired}
            for name, st in self._state.items()
        }


# ------------------------------------------------------------- default rules --

def default_rules(
    *,
    feed_stale_after_s: float = 30.0,
    disk_min_free_mb: float = 500.0,
    disk_path: str = ".",
    mt5_disconnect_threshold: int = 5,
) -> list[AlertRule]:
    """Startup rules per ТЗ 6.2 (FEED_STALE / CIRCUIT_BREAKER / DISK_LOW /
    MT5_DISCONNECT) with default cooldowns from the ТЗ config example."""
    return [
        AlertRule(
            rule_name="FEED_STALE",
            condition=lambda ctx: _age_s(ctx.get("last_tick_age_s")) > feed_stale_after_s,
            cooldown_sec=600,
            severity="P1",
            description=f"feed stale: last tick older than {feed_stale_after_s:.0f}s",
        ),
        AlertRule(
            rule_name="CIRCUIT_BREAKER",
            condition=lambda ctx: bool(ctx.get("circuit_breaker")),
            cooldown_sec=3600,
            severity="P0",
            description="risk circuit breaker tripped",
        ),
        AlertRule(
            rule_name="DISK_LOW",
            condition=lambda ctx: _disk_low(disk_path, disk_min_free_mb),
            cooldown_sec=1800,
            severity="P1",
            description=f"free disk below {disk_min_free_mb:.0f} MB",
        ),
        AlertRule(
            rule_name="MT5_DISCONNECT",
            condition=lambda ctx: _int(ctx.get("mt5_disconnect_count")) >= mt5_disconnect_threshold,
            cooldown_sec=600,
            severity="P1",
            description=f"symbol_info_tick None {mt5_disconnect_threshold}x in a row",
        ),
    ]


def _age_s(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _disk_low(path: str, min_free_mb: float) -> bool:
    try:
        return disk_status(path).free_mb < float(min_free_mb)
    except OSError:
        return False


# ------------------------------------------------------------------ wiring --

def build_from_config(cfg: dict) -> AlertManager | None:
    """Build an AlertManager from ``monitoring.alerts`` config, or None when
    disabled (default). Never raises: alert wiring must not break the bot."""
    cfg_alerts = ((cfg or {}).get("monitoring", {}) or {}).get("alerts", {}) or {}
    if not cfg_alerts.get("enabled"):
        return None
    notifier = None
    if cfg_alerts.get("telegram", True):
        notifier = _telegram_notifier()
    return AlertManager(
        default_rules(
            feed_stale_after_s=float(cfg_alerts.get("feed_stale_after_s", 30.0)),
            disk_min_free_mb=float(cfg_alerts.get("disk_min_free_mb", 500.0)),
            disk_path=str(cfg_alerts.get("disk_path", ".")),
            mt5_disconnect_threshold=int(cfg_alerts.get("mt5_disconnect_threshold", 5)),
        ),
        notifier,
    )


def _telegram_notifier() -> Notifier:
    """Lazy Telegram notifier built on alerts.telegram_bot (injected lazily so
    unit tests and disabled configs never import network code)."""

    def _send(rule_name: str, severity: str, message: str) -> None:
        from alerts.telegram_bot import TelegramAlertBot
        from config.loader import load_config

        bot = TelegramAlertBot(load_config())
        bot.send_text_message(f"⚠️ [{severity}] {rule_name}: {message}")

    return _send
