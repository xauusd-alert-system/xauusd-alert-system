"""Tests for the audit W11 fix in alerts/telegram_bot.py: the bot token must be
redacted from logged exception messages (requests exceptions embed the request
URL, which contains the token, and logging it would leak control of the bot)."""
import pytest

from alerts.telegram_bot import TelegramAlertBot


def test_redact_strips_token_from_url_and_exception():
    bot = TelegramAlertBot(cfg={}, bot_token="SECRETTOKEN123", chat_id="1")
    msg = "https://api.telegram.org/botSECRETTOKEN123/sendMessage Failed: SECRETTOKEN123"
    redacted = bot._redact(msg)
    assert "SECRETTOKEN123" not in redacted
    assert "<REDACTED>" in redacted


def test_redact_noop_without_token():
    bot = TelegramAlertBot(cfg={}, bot_token=None, chat_id="1")
    msg = "plain error with no token"
    assert bot._redact(msg) == msg


def test_base_url_has_no_extra_braces():
    """W11: the base_url must be a valid URL (no stray literal braces around the
    bot path that would make requests.post fail)."""
    bot = TelegramAlertBot(cfg={}, bot_token="TOKEN", chat_id="1")
    assert bot.base_url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert "{{" not in bot.base_url and "}}" not in bot.base_url
