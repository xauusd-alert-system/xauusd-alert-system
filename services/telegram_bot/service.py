"""Telegram Bot service (TZ 8.2) — standalone alerting process.

Thin wrapper around ``alerts/telegram_bot.py`` (``TelegramAlertBot``) and
``alerts/control_bot.py`` (``TelegramControlBot``). This module holds no
Telegram logic of its own; it hosts the bot with a simple health endpoint:

* ``process_alive``  — the service main thread / bot host is running;
* ``telegram_token`` — ``TELEGRAM_BOT_TOKEN`` is configured (config/env
  presence only — NO real Telegram API call from the health check).

An in-process ``queue.Queue`` (``EVENT_QUEUE``) is provided as the minimal
buffer between producers (trader / pipelines) and the bot host; the bot drains
it and sends via the existing ``TelegramAlertBot.send_text_message``.

Note: the ``--alerts-only`` trader mode (TZ 8.2) is NOT implemented here —
that is a Phase-4 task on ``scripts/run_bot.py``.

Run: ``python -m services.telegram_bot [--health-port 8792]``
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.base import start_health_server_thread  # noqa: E402

DEFAULT_HEALTH_PORT = 8792

SERVICE_NAME = "telegram_bot"

# Simple cross-thread event buffer for messages produced inside this process
# (cross-process producers will switch to a sqlite-backed queue later).
EVENT_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=1000)


def enqueue_text(text: str) -> bool:
    """Buffer one message for the bot host. Drops the oldest when full."""
    try:
        EVENT_QUEUE.put_nowait(text)
        return True
    except queue.Full:
        try:
            EVENT_QUEUE.get_nowait()
            EVENT_QUEUE.put_nowait(text)
            return True
        except (queue.Empty, queue.Full):
            return False


# Sentinel: "resolve from the environment" (distinct from an explicit None,
# which means "definitely not configured").
UNSET = object()


def make_token_check(bot_token=UNSET, chat_id=UNSET) -> Callable[[], tuple[bool, str]]:
    """Health check: Telegram credentials configured (no API call).

    Default (``UNSET``) resolves ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``
    via the existing ``config.loader.get_env``. An explicit ``None`` (or "")
    means "not configured" -> degraded, never a crash.
    """

    def check() -> tuple[bool, str]:
        from config.loader import get_env

        token = get_env("TELEGRAM_BOT_TOKEN", required=False) if bot_token is UNSET else bot_token
        chat = get_env("TELEGRAM_CHAT_ID", required=False) if chat_id is UNSET else chat_id
        if not token:
            return False, "TELEGRAM_BOT_TOKEN is not configured (degraded)"
        if not chat:
            return False, "TELEGRAM_CHAT_ID is not configured (degraded)"
        return True, "ok (credentials configured, no API call)"

    return check


def make_process_alive_check() -> Callable[[], tuple[bool, str]]:
    """Health check: this service process is up (always ok while serving)."""

    def check() -> tuple[bool, str]:
        return True, f"ok (pid={os.getpid()})"

    return check


def build_checks(bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> dict:
    """Assemble the service checks dict (unit-tested without the network)."""
    return {
        "process_alive": make_process_alive_check(),
        "telegram_token": make_token_check(bot_token, chat_id),
    }


def run(args: argparse.Namespace) -> None:
    """Entry point: health server + bot host loop.

    Starts the health endpoint, constructs the existing ``TelegramAlertBot``
    (fail-safe: it stays inert without credentials) and drains the event
    queue into it. The interactive control bot (``TelegramControlBot``) needs
    a live trader object and therefore stays inside ``scripts/run_bot.py``
    until the Phase-4 service split; this process only sends alerts.
    """
    from alerts.telegram_bot import TelegramAlertBot

    checks = build_checks()
    server = start_health_server_thread(args.health_port, checks)

    from config.loader import load_config

    bot = TelegramAlertBot(load_config())

    print(f"[{os.getpid()}] telegram_bot service up (health: http://127.0.0.1:{args.health_port}/health)")
    try:
        while True:
            try:
                text = EVENT_QUEUE.get(timeout=1.0)
            except queue.Empty:
                continue
            if text == _SHUTDOWN_SENTINEL:
                break
            ok = bot.send_text_message(text)
            print(f"[{os.getpid()}] send: {'ok' if ok else 'failed'}")
    finally:
        server.should_exit = True


_SHUTDOWN_SENTINEL = "\x00shutdown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {__name__.rsplit('.', 1)[0]}",
        description="Telegram bot service (TZ 8.2): alert sender host with a "
        "health endpoint (token-configured check, no API calls).",
    )
    parser.add_argument("--health-port", type=int, default=DEFAULT_HEALTH_PORT)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
