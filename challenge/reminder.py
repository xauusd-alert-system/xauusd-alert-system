"""python -m challenge.reminder — send a Telegram reminder to start the
HashHedge challenge. Used by the scheduled task (schtasks) at 18:25 local.
"""

import sys

from config.loader import load_config

from alerts.telegram_bot import TelegramAlertBot

MESSAGE = (
    "⏰ Напоминание: через 5 минут стартует NYSE-сессия (18:30-00:55).\n\n"
    "Для проп-челленджа HashHedge:\n"
    "1. Проверь, что SnowVPN включён\n"
    "2. Открой терминал и нажми «Начать торговлю»\n"
    "3. Скажи мне — запущу бота"
)


def main():
    cfg = load_config()
    bot = TelegramAlertBot(cfg)
    if not bot.send_text_message(MESSAGE):
        print("FAILED: telegram send")
        return 1
    print("OK: reminder sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())