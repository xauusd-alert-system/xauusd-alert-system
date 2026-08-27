"""Services package (Phase 3, Step 10 — TZ 8.1, 8.2, 8.8).

Independent long-running processes, each with a shared HTTP health endpoint:

* ``services.ledger_bridge``  — Signal Desk outbox delivery (TZ 8.1);
* ``services.telegram_bot``   — Telegram alert/control bot host (TZ 8.2);
* ``services.news_feed``      — economic-calendar cache refresher (TZ 8.8).

Every service is a thin wrapper around existing project modules
(``data/ledger_bridge.py``, ``alerts/telegram_bot.py`` + ``alerts/control_bot.py``,
``news/calendar_feed.py``) — no business logic is duplicated here. Entry
points: ``python -m services.<name>``, health: ``GET /health`` on the
configured port (config.yaml ``services`` section).
"""
