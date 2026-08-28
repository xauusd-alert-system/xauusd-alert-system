"""ТЗ 10.3 — Telegram admin whitelist (TELEGRAM_ADMIN_IDS) for control commands."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alerts.control_bot import TelegramControlBot, parse_admin_ids
from config.loader import load_config

CFG = load_config()


def _make_bot(monkeypatch, admin_id="", admin_ids=None):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ADMIN_IDS", raising=False)
    if admin_id:
        monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", admin_id)
    if admin_ids:
        monkeypatch.setenv("TELEGRAM_ADMIN_IDS", admin_ids)
    trader = NS(magic_number=1, dry_run=False, pipelines={}, cfg=CFG)
    sent: list[tuple[str, str]] = []
    bot = TelegramControlBot(trader)
    bot._send = lambda chat_id, text, **kw: sent.append((chat_id, text))
    return bot, sent


def test_admin_ids_parsed_from_env(monkeypatch):
    assert parse_admin_ids("111, 222,333") == frozenset({"111", "222", "333"})
    assert parse_admin_ids("") == frozenset()
    assert parse_admin_ids(None) == frozenset()
    # non-numeric garbage dropped
    assert parse_admin_ids("111,abc,,42x") == frozenset({"111"})
    bot, _ = _make_bot(monkeypatch, admin_ids="111,222")
    assert bot.admin_ids == frozenset({"111", "222"})


def test_control_command_allowed_for_admin_whitelist(monkeypatch):
    bot, sent = _make_bot(monkeypatch, admin_ids="555,666")
    assert bot._is_admin("555") is True
    assert bot._is_admin("666") is True
    bot._dispatch("/pause", "666", ())
    assert sent and "Unauthorised" not in sent[-1][1]


def test_control_command_rejected_for_non_admin(monkeypatch):
    bot, sent = _make_bot(monkeypatch, admin_ids="555,666")
    assert bot._is_admin("999") is False
    bot._dispatch("/closeall", "999", ())
    assert sent and "Unauthorised" in sent[-1][1]


def test_admin_chat_id_still_works_alongside_whitelist(monkeypatch):
    bot, _ = _make_bot(monkeypatch, admin_id="42", admin_ids="555")
    assert bot._is_admin("42") is True
    assert bot._is_admin("555") is True
    assert bot._is_admin("43") is False


def test_no_config_still_fails_closed(monkeypatch):
    """The whitelist never relaxes the fail-closed no-config refusal."""
    bot, sent = _make_bot(monkeypatch)
    assert bot._is_admin("42") is False
    bot._dispatch("/pause", "42", ())
    assert sent and "Unauthorised" in sent[-1][1]
