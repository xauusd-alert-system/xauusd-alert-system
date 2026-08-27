"""ТЗ 6.2: AlertManager rules + cooldowns tests.

Covers:
    - rule_triggers_callback            — firing rule invokes the notifier;
    - cooldown_suppresses_repeats       — repeats within cooldown suppressed;
    - disk_check_thresholds             — monitoring/disk.py thresholds;
    - feed_stale_rule                   — FEED_STALE threshold behaviour;
    - config_disabled_no_side_effects   — disabled config -> None manager.
"""
from __future__ import annotations

import shutil

import pytest

from monitoring.alerts import AlertManager, AlertRule, build_from_config, default_rules
from monitoring.disk import check_disk_space, disk_status


@pytest.fixture
def notifier():
    calls: list[tuple[str, str, str]] = []

    def _send(rule_name: str, severity: str, message: str) -> None:
        calls.append((rule_name, severity, message))

    _send.calls = calls  # type: ignore[attr-defined]
    return _send


# --------------------------------------------------------- rule fires callback

def test_rule_triggers_callback(notifier):
    rule = AlertRule("CIRCUIT_BREAKER", lambda ctx: ctx.get("circuit_breaker") is True,
                     cooldown_sec=3600, severity="P0", description="CB tripped")
    am = AlertManager([rule], notifier)

    fired = am.evaluate({"circuit_breaker": True})

    assert fired == ["CIRCUIT_BREAKER"]
    assert notifier.calls == [("CIRCUIT_BREAKER", "P0", "CB tripped")]


def test_rule_not_triggered_no_callback(notifier):
    rule = AlertRule("CIRCUIT_BREAKER", lambda ctx: False, cooldown_sec=60)
    am = AlertManager([rule], notifier)
    assert am.evaluate({"circuit_breaker": True}) == []
    assert notifier.calls == []


def test_notifier_exception_swallowed(notifier):
    def _boom(*_a):
        raise RuntimeError("telegram down")

    rule = AlertRule("X", lambda ctx: True, cooldown_sec=60)
    am = AlertManager([rule], _boom)
    assert am.evaluate({}) == ["X"]  # fired despite notifier failure


def test_broken_rule_condition_does_not_crash(notifier):
    def _bad(_ctx):
        raise ValueError("boom")

    ok = AlertRule("OK", lambda ctx: True, cooldown_sec=0)
    am = AlertManager([AlertRule("BAD", _bad, cooldown_sec=0), ok], notifier)
    assert am.evaluate({}) == ["OK"]


# ------------------------------------------------------------- cooldown logic

def test_cooldown_suppresses_repeats(notifier):
    rule = AlertRule("FEED_STALE", lambda ctx: True, cooldown_sec=600)
    am = AlertManager([rule], notifier)

    assert am.evaluate({}) == ["FEED_STALE"]          # first fires
    assert am.evaluate({}) == []                      # inside cooldown
    assert am.evaluate({}) == []                      # still inside
    stats = am.rule_stats()["FEED_STALE"]
    assert stats["fires"] == 1
    assert stats["suppressed"] == 2


def test_cooldown_expires_allows_refire():
    t = {"now": 1000.0}
    rule = AlertRule("R", lambda ctx: True, cooldown_sec=600)
    am = AlertManager([rule], None, clock=lambda: t["now"])

    assert am.evaluate({}) == ["R"]
    t["now"] += 599
    assert am.evaluate({}) == []
    t["now"] += 2  # 601s later
    assert am.evaluate({}) == ["R"]


def test_per_rule_cooldowns_are_independent(notifier):
    hot = AlertRule("HOT", lambda ctx: True, cooldown_sec=0)
    cold = AlertRule("COLD", lambda ctx: True, cooldown_sec=3600)
    am = AlertManager([hot, cold], notifier)

    assert am.evaluate({}) == ["HOT", "COLD"]
    assert am.evaluate({}) == ["HOT"]  # only the no-cooldown rule refires


# ------------------------------------------------------------ default rules --

def test_feed_stale_rule():
    rules = {r.rule_name: r for r in default_rules(feed_stale_after_s=30.0)}
    fresh = rules["FEED_STALE"]
    assert fresh.condition({"last_tick_age_s": 10}) is False
    assert fresh.condition({"last_tick_age_s": 31}) is True
    assert fresh.condition({}) is False  # no data -> not stale


def test_mt5_disconnect_rule_threshold():
    rules = {r.rule_name: r for r in default_rules(mt5_disconnect_threshold=5)}
    rule = rules["MT5_DISCONNECT"]
    assert rule.condition({"mt5_disconnect_count": 4}) is False
    assert rule.condition({"mt5_disconnect_count": 5}) is True
    assert rule.condition({}) is False


def test_circuit_breaker_rule():
    rules = {r.rule_name: r for r in default_rules()}
    rule = rules["CIRCUIT_BREAKER"]
    assert rule.condition({"circuit_breaker": True}) is True
    assert rule.condition({"circuit_breaker": False}) is False


def test_disk_low_rule_uses_real_path(tmp_path):
    free_mb = disk_status(str(tmp_path)).free_mb
    # Threshold just above actual free space -> triggers.
    rules = {r.rule_name: r for r in default_rules(
        disk_path=str(tmp_path), disk_min_free_mb=free_mb + 1)}
    assert rules["DISK_LOW"].condition({}) is True
    # Threshold at/below actual free space -> no alert.
    rules_ok = {r.rule_name: r for r in default_rules(
        disk_path=str(tmp_path), disk_min_free_mb=free_mb)}
    assert rules_ok["DISK_LOW"].condition({}) is False


# ------------------------------------------------------------- disk checker --

def test_disk_check_thresholds(tmp_path):
    status = disk_status(str(tmp_path))
    assert status.free_mb >= 0 and status.total_mb > 0
    assert 0.0 <= status.pct_used <= 100.0
    # A threshold above actual free space fails; zero always passes.
    assert check_disk_space(str(tmp_path), min_free_mb=status.free_mb + 1) is False
    assert check_disk_space(str(tmp_path), min_free_mb=0) is True


# --------------------------------------------------------------- config gate --

def test_config_disabled_no_side_effects(notifier, monkeypatch):
    """monitoring.alerts.enabled missing/false -> no manager, no Telegram import."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert build_from_config({}) is None
    assert build_from_config({"monitoring": {"alerts": {"enabled": False}}}) is None


def test_config_enabled_builds_manager(monkeypatch):
    # Telegram import must not even be attempted with telegram: false.
    monkeypatch.setitem(
        __import__("sys").modules, "alerts.telegram_bot", None,
    )
    mgr = build_from_config({
        "monitoring": {"alerts": {
            "enabled": True, "telegram": False,
            "feed_stale_after_s": 45, "disk_min_free_mb": 250,
        }},
    })
    assert mgr is not None
    assert set(mgr.rules) == {"FEED_STALE", "CIRCUIT_BREAKER", "DISK_LOW",
                              "MT5_DISCONNECT"}
    assert mgr.notifier is None


def test_config_enabled_with_telegram_notifies(monkeypatch):
    sent: list[str] = []

    class _FakeBot:
        def __init__(self, _cfg):
            pass

        def send_text_message(self, text: str) -> bool:
            sent.append(text)
            return True

    import alerts.telegram_bot as tb

    monkeypatch.setattr(tb, "TelegramAlertBot", _FakeBot)
    mgr = build_from_config({"monitoring": {"alerts": {"enabled": True}}})
    assert mgr is not None and mgr.notifier is not None
    mgr.evaluate({"circuit_breaker": True})
    assert sent and "CIRCUIT_BREAKER" in sent[0]
