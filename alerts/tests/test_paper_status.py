from types import SimpleNamespace as NS

from alerts.control_bot import TelegramControlBot


def test_paper_command_is_admin_only_liveness_without_outcomes(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "42")
    monkeypatch.setenv("PAPER_MANIFEST_PATH", "frozen.json")
    monkeypatch.setenv("PAPER_LEDGER_DB", "paper.sqlite")
    trader = NS(cfg={}, dry_run=True, pipelines={})
    bot = TelegramControlBot(trader)
    sent = []
    monkeypatch.setattr(bot, "_send", lambda chat_id, text, parse_mode="": sent.append(text))
    monkeypatch.setattr(
        "paper.accumulator.load_frozen_manifest",
        lambda path, verify_model=False: {"run_id": "run-1"},
    )
    status = {
        "run_id": "run-1",
        "asset_key": "XAUUSD",
        "variant": "wide_trend_filtered",
        "mode": "paper_frozen",
        "source": "append_only_paper_ledger",
        "manifest_sha256": "a" * 64,
        "signals": 12,
        "opened_trades": 7,
        "closed_trades": 6,
        "minimum_closed_trades": 50,
        "ready_for_one_time_validation": False,
        "latest_bar_timestamp_utc": 123,
    }
    monkeypatch.setattr("data.paper_ledger.paper_accumulation_status", lambda db, run: status)

    bot._dispatch("/paper", "42")
    assert "Closed: 6/50" in sent[-1]
    assert "P&L" not in sent[-1] and "PF" not in sent[-1]

    bot._dispatch("/paper", "not-admin")
    assert "Unauthorised" in sent[-1]
