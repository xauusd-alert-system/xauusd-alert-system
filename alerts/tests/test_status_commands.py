"""
Tests for the read-only Telegram status commands (/status, /why, /metrics
today|week, /account) — alerts/status_commands.py + their registration and
authorization in alerts/control_bot.py.

Everything runs on mocks/SimpleNamespace: no real MT5 terminal, no network to
the Telegram Bot API (CI has neither). The MetaTrader5 module handle is faked
by monkeypatching alerts.status_commands._mt5; Telegram sends are captured by
stubbing TelegramControlBot._send.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from alerts import status_commands as sc
from alerts.control_bot import TelegramControlBot

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CFG = {
    "assets": {
        "XAUUSD": {"mt5_symbol": "GOLD"},                 # point_value_lot -> default 100.0
        "EURUSD": {"mt5_symbol": "EURUSD", "point_value_lot": 100000},
    },
    "backtest": {},
}

ENTRY_CTX = {
    "asset_key": "XAUUSD",
    "opened_at_utc": "2026-08-08T09:15:00+00:00",
    "bias": "long",
    "confidence": 0.73,
    "regime": "trend_up",
    "reasoning_summary": "ML long 0.73 + rule vote совпали; тренд вверх, лонг по откату в зону входа.",
    "entry_zone": [2001.5, 2002.5],
    "invalidation": 1990.0,
    "targets": [2006.0, 2010.0, 2014.0],
    "session": "london",
}


def make_position(**kw):
    base = dict(
        ticket=101, symbol="GOLD", type=0, volume=0.01,
        price_open=2000.0, price_current=2010.0, profit=10.0,
        time=int(datetime.now(timezone.utc).timestamp()) - 5400,  # 1h30m ago
        sl=1998.0, tp=2014.0, magic=777111,
    )
    base.update(kw)
    return NS(**base)


def make_deal(**kw):
    base = dict(
        ticket=1, position_id=101, symbol="GOLD", type=1, entry=1,
        volume=0.01, price=2010.0, profit=50.0, swap=0.0, commission=0.0,
        magic=777111, comment="", time=int(datetime.now(timezone.utc).timestamp()) - 600,
    )
    base.update(kw)
    return NS(**base)


def write_contexts(tmp_path, mapping):
    path = tmp_path / "live_positions.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return str(path)


class CapturedSends:
    def __init__(self):
        self.messages = []

    def __call__(self, chat_id, text, parse_mode=""):
        self.messages.append((chat_id, text))


def make_bot(monkeypatch, admin_id="4242"):
    """TelegramControlBot wired to a fake trader, with captured sends."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", admin_id)
    trader = NS(magic_number=777111, dry_run=False, pipelines={"XAUUSD": object()}, cfg=CFG)
    bot = TelegramControlBot(trader)
    captured = CapturedSends()
    monkeypatch.setattr(bot, "_send", captured)
    return bot, captured


def last_text(captured):
    assert captured.messages, "expected at least one outgoing message"
    return captured.messages[-1][1]


# ---------------------------------------------------------------------------
# ensure_mt5_connection (ТЗ B3): never raises, False when initialize() fails
# ---------------------------------------------------------------------------

def test_ensure_mt5_connection_true_when_terminal_info_present(monkeypatch):
    monkeypatch.setattr(sc, "_mt5", NS(terminal_info=lambda: {"connected": True}))
    assert sc.ensure_mt5_connection() is True


def test_ensure_mt5_connection_falls_back_to_initialize(monkeypatch):
    monkeypatch.setattr(sc, "_mt5", NS(terminal_info=lambda: None, initialize=lambda: True))
    assert sc.ensure_mt5_connection() is True


def test_ensure_mt5_connection_false_when_initialize_fails(monkeypatch):
    monkeypatch.setattr(sc, "_mt5", NS(terminal_info=lambda: None, initialize=lambda: False))
    assert sc.ensure_mt5_connection() is False


def test_ensure_mt5_connection_false_when_package_missing(monkeypatch):
    monkeypatch.setattr(sc, "_mt5", None)
    monkeypatch.setattr(sc, "_mt5_import_failed", True)
    assert sc.ensure_mt5_connection() is False


# ---------------------------------------------------------------------------
# /status formatting (ТЗ B4): symbol, direction, P&L in $ and R
# ---------------------------------------------------------------------------

def test_format_status_report_contains_direction_pnl_and_r():
    # entry 2000, stop 1990, volume 0.01, pvl 100 -> risk $10; profit +$10 -> +1.00 R
    fixed_now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    pos = make_position(time=int(fixed_now.timestamp()) - 5400)  # opened 1h30m ago
    contexts = {"101": ENTRY_CTX}
    msg = sc.format_status_report(
        NS(balance=10000.0, equity=10010.0, profit=10.0),
        [pos], contexts, CFG, dry_run=False, n_assets=1, now=fixed_now,
    )
    assert "BUY" in msg
    assert "XAUUSD" in msg and "GOLD" in msg  # internal key + MT5 symbol
    assert "+10.00" in msg                     # floating P&L in account currency
    assert "+1.00 R" in msg                    # floating P&L in R (ТЗ formula)
    assert "1h 30m" in msg                     # time in trade
    assert "trend_up" in msg                   # regime at entry from the context


def test_format_status_report_no_positions():
    msg = sc.format_status_report(
        NS(balance=10000.0, equity=10000.0, profit=0.0),
        [], {}, CFG,
    )
    assert "Открытых позиций нет" in msg


def test_format_status_report_r_unavailable_without_context():
    msg = sc.format_status_report(None, [make_position()], {}, CFG)
    assert "n/a" in msg          # R is never invented when the stop is unknown
    assert "+10.00" in msg       # $ P&L is still shown


def test_floating_r_matches_backtest_formula():
    # R = pnl / (|entry - initial_stop| * volume * point_value_lot)
    # |2000-1990| * 0.01 * 100 = $10 risk -> R = net pnl / 10
    assert sc.floating_r(10.0, 2000.0, 1990.0, 0.01, 100.0) == pytest.approx(1.0)
    assert sc.floating_r(-5.0, 2000.0, 1990.0, 0.01, 100.0) == pytest.approx(-0.5)
    # EURUSD example: |1.1000-1.0900| * 0.01 * 100000 = $10 risk -> pnl $2.50 = +0.25R
    assert sc.floating_r(2.5, 1.1, 1.09, 0.01, 100000) == pytest.approx(0.25)
    assert sc.floating_r(1.0, 2000.0, 2000.0, 0.01, 100.0) is None  # zero risk base
    assert sc.floating_r(1.0, 2000.0, None, 0.01, 100.0) is None    # unknown stop


# ---------------------------------------------------------------------------
# /why (ТЗ B4): context present / missing / no position
# ---------------------------------------------------------------------------

def test_why_with_context_prints_reasoning_verbatim():
    pos = make_position()
    msg = sc.format_why_report("XAUUSD", "GOLD", pos, ENTRY_CTX)
    assert ENTRY_CTX["reasoning_summary"] in msg      # дословно
    assert "73.0%" in msg                              # confidence 0.73 -> %
    assert "trend_up" in msg and "london" in msg       # regime + session
    assert "2001.5" in msg and "2002.5" in msg         # entry zone
    assert "1990" in msg                               # invalidation
    assert "2006" in msg and "2010" in msg and "2014" in msg  # targets
    assert "2026-08-08" in msg                         # opened_at_utc


def test_why_without_context_says_unavailable_and_does_not_fabricate():
    pos = make_position()
    msg = sc.format_why_report("XAUUSD", "GOLD", pos, None)
    assert "контекст входа недоступен" in msg.lower() or "недоступен" in msg
    assert ENTRY_CTX["reasoning_summary"] not in msg


def test_why_without_position_reports_none_open():
    msg = sc.format_why_report("XAUUSD", "GOLD", None, None)
    assert "Нет открытой позиции" in msg
    assert "XAUUSD" in msg and "GOLD" in msg


# ---------------------------------------------------------------------------
# /metrics (ТЗ B4 + C3): numeric WR / PF / expectancy cases
# ---------------------------------------------------------------------------

def _metrics_deals():
    now_ts = int(datetime.now(timezone.utc).timestamp())
    return [
        make_deal(ticket=1, position_id=101, entry=0, profit=0.0, price=2000.0,
                  time=now_ts - 7000),                      # IN — excluded from trade stats
        make_deal(ticket=2, position_id=101, entry=1, profit=50.0, price=2010.0,
                  time=now_ts - 6000),                      # win +50
        make_deal(ticket=3, position_id=102, entry=1, profit=-20.0,
                  time=now_ts - 5000),                      # loss -20
        make_deal(ticket=4, position_id=103, entry=1, profit=10.0, swap=-2.0,
                  commission=-3.0, time=now_ts - 4000),     # win 10-2-3 = +5
    ]


def test_compute_deal_metrics_wr_pf_expectancy_numeric():
    m = sc.compute_deal_metrics(_metrics_deals())
    assert m["n"] == 3                       # IN deal excluded (any non-IN = realization)
    assert m["wins"] == 2 and m["losses"] == 1
    assert m["win_rate_pct"] == pytest.approx(100 * 2 / 3)
    # gross profit = 50 + 5 = 55, gross loss = 20 -> PF = 2.75
    assert m["profit_factor"] == pytest.approx(2.75)
    assert m["total_pnl"] == pytest.approx(35.0)             # 50 - 20 + 5
    assert m["expectancy"] == pytest.approx(35.0 / 3)


def test_compute_deal_metrics_profit_factor_edge_cases():
    only_wins = [make_deal(entry=1, profit=10.0)]
    assert sc.compute_deal_metrics(only_wins)["profit_factor"] == float("inf")
    only_losses = [make_deal(entry=1, profit=-10.0)]
    assert sc.compute_deal_metrics(only_losses)["profit_factor"] == pytest.approx(0.0)
    assert sc.compute_deal_metrics([])["profit_factor"] is None
    assert sc.compute_deal_metrics([])["n"] == 0


def test_compute_deal_metrics_r_only_where_risk_known():
    deals = _metrics_deals()
    # Only position 101 still has an entry context (e.g. partial close):
    # IN price 2000, invalidation 1990, vol 0.01, pvl 100 -> risk $10, pnl +50 -> +5R
    contexts = {"101": {**ENTRY_CTX, "invalidation": 1990.0}}
    m = sc.compute_deal_metrics(deals, contexts=contexts, cfg=CFG)
    assert m["n_r"] == 1                        # purged contexts -> no R for the rest
    assert m["mean_r"] == pytest.approx(5.0)


def test_format_metrics_report_totals_and_per_asset():
    msg = sc.format_metrics_report(_metrics_deals(), {}, CFG, "сегодня (UTC)")
    assert "сегодня (UTC)" in msg
    assert "WR 66.7%" in msg
    assert "PF 2.75" in msg
    assert "+35.00" in msg
    assert "XAUUSD (GOLD)" in msg               # per-asset line uses internal key


def test_format_metrics_report_no_deals():
    msg = sc.format_metrics_report([], {}, CFG, "последние 7 дней (UTC)")
    assert "Закрытых сделок за период нет" in msg


def test_fetch_deals_between_filters_client_side(monkeypatch):
    now = datetime.now(timezone.utc)
    inside = make_deal(time=int((now - timedelta(hours=1)).timestamp()))
    outside = make_deal(ticket=99, time=int((now - timedelta(days=10)).timestamp()))

    def fake_history_deals_get(*args, **kwargs):
        return [inside, outside]  # real-API style call accepted

    monkeypatch.setattr(sc, "_mt5", NS(history_deals_get=fake_history_deals_get))
    got = sc.fetch_deals_between(now - timedelta(hours=2), now)
    assert [d.ticket for d in got] == [inside.ticket]


def test_fetch_deals_between_shim_fallback(monkeypatch):
    """The bundled shim binds the first positional arg to `position` and knows
    no date ranges -> returns None for the dated call; we must then re-fetch
    everything (shim exposes _inject) and filter by time client-side."""
    now = datetime.now(timezone.utc)
    inside = make_deal(time=int((now - timedelta(hours=1)).timestamp()))
    outside = make_deal(ticket=99, time=int((now - timedelta(days=10)).timestamp()))

    def fake_history_deals_get(*args, **kwargs):
        if args:                      # dated call -> shim misinterprets, no match
            return None
        return [inside, outside]      # unfiltered fallback

    fake_module = NS(history_deals_get=fake_history_deals_get, _inject=lambda *a: None)
    monkeypatch.setattr(sc, "_mt5", fake_module)
    got = sc.fetch_deals_between(now - timedelta(hours=2), now)
    assert [d.ticket for d in got] == [inside.ticket]


def test_period_range_today_and_week():
    now = datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc)
    dt_from, dt_to, label = sc.period_range("today", now=now)
    assert (dt_from, dt_to) == (datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc), now)
    assert "сегодня" in label
    dt_from, dt_to, label = sc.period_range("week", now=now)
    assert dt_to - dt_from == timedelta(days=7)
    assert "7" in label


# ---------------------------------------------------------------------------
# /account formatting (ТЗ B4)
# ---------------------------------------------------------------------------

def test_format_account_report_full(monkeypatch):
    info = NS(balance=10000.0, equity=9900.0, margin=100.0,
              margin_free=9800.0, margin_level=9900.0, profit=-100.0)
    msg = sc.format_account_report(info, realized_today=-55.0)
    assert "10,000.00" in msg          # balance
    assert "9,900.00" in msg           # equity
    assert "-100.00" in msg            # floating = equity - balance
    assert "-55.00" in msg             # realized today
    assert "9,900.0%" in msg           # margin level


def test_format_account_report_no_margin_shows_dash():
    info = NS(balance=100.0, equity=100.0, margin=0.0,
              margin_free=100.0, margin_level=0.0, profit=0.0)
    msg = sc.format_account_report(info, realized_today=0.0)
    assert "—" in msg                  # margin level n/a, not a bogus 0.0%


def test_format_account_report_missing_info():
    assert "недоступны" in sc.format_account_report(None, realized_today=0.0)


# ---------------------------------------------------------------------------
# Authorization (ТЗ B2 + C4): foreign chats get refused BEFORE any MT5 access
# ---------------------------------------------------------------------------

def test_foreign_chat_is_refused_and_mt5_never_touched(monkeypatch):
    bot, captured = make_bot(monkeypatch, admin_id="4242")

    def boom(*a, **k):
        raise AssertionError("MT5 must not be touched for unauthorized chats")

    monkeypatch.setattr(sc, "_mt5", NS(
        terminal_info=boom, initialize=boom, account_info=boom,
        positions_get=boom, history_deals_get=boom,
    ))
    for cmd, args in (("/status", ()), ("/why", ("XAUUSD",)), ("/metrics", ("today",)),
                      ("/account", ()), ("/positions", ()), ("/pause", ()),
                      ("/closeall", ())):
        captured.messages.clear()
        bot._dispatch(cmd, "999999", args)   # foreign chat_id
        assert "Unauthorised" in last_text(captured), cmd


def test_admin_fallback_to_telegram_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "777")
    trader = NS(magic_number=1, dry_run=False, pipelines={}, cfg=CFG)
    bot = TelegramControlBot(trader)
    assert bot.admin_id == "777"
    assert bot._is_admin("777") and not bot._is_admin("778")


def test_unconfigured_admin_fails_closed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("TELEGRAM_ADMIN_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    trader = NS(magic_number=1, dry_run=False, pipelines={}, cfg=CFG)
    bot = TelegramControlBot(trader)
    assert bot._is_admin("4242") is False  # nobody is admin -> nothing sensitive served


# ---------------------------------------------------------------------------
# Handler integration via the bot dispatcher (fake MT5 + tmp context file)
# ---------------------------------------------------------------------------

def test_status_command_end_to_end(monkeypatch, tmp_path):
    bot, captured = make_bot(monkeypatch)
    path = write_contexts(tmp_path, {"101": ENTRY_CTX})
    monkeypatch.setattr(sc, "LIVE_POSITIONS_PATH", path)
    monkeypatch.setattr(sc, "_mt5", NS(
        terminal_info=lambda: {"connected": True},
        account_info=lambda: NS(balance=10000.0, equity=10010.0, profit=10.0),
        positions_get=lambda **kw: [make_position()],
    ))
    bot._dispatch("/status", "4242", ())
    msg = last_text(captured)
    assert "BUY" in msg and "+10.00" in msg and "+1.00 R" in msg


def test_status_command_mt5_unavailable(monkeypatch):
    bot, captured = make_bot(monkeypatch)
    monkeypatch.setattr(sc, "_mt5", NS(terminal_info=lambda: None, initialize=lambda: False))
    bot._dispatch("/status", "4242", ())
    assert "MT5 терминал недоступен" in last_text(captured)


def test_why_command_end_to_end(monkeypatch, tmp_path):
    bot, captured = make_bot(monkeypatch)
    path = write_contexts(tmp_path, {"101": ENTRY_CTX})
    monkeypatch.setattr(sc, "LIVE_POSITIONS_PATH", path)
    monkeypatch.setattr(sc, "_mt5", NS(
        terminal_info=lambda: {"connected": True},
        positions_get=lambda symbol=None, **kw: [make_position()] if symbol == "GOLD" else [],
    ))
    # also exercise argument parsing (/why xauusd, lowercase)
    bot._handle_update({"message": {"chat": {"id": 4242}, "text": "/why xauusd"}})
    msg = last_text(captured)
    assert ENTRY_CTX["reasoning_summary"] in msg
    assert "Почему открыта позиция" in msg


def test_why_command_no_position(monkeypatch, tmp_path):
    bot, captured = make_bot(monkeypatch)
    monkeypatch.setattr(sc, "LIVE_POSITIONS_PATH", write_contexts(tmp_path, {}))
    monkeypatch.setattr(sc, "_mt5", NS(
        terminal_info=lambda: {"connected": True},
        positions_get=lambda symbol=None, **kw: None,
    ))
    bot._dispatch("/why", "4242", ("XAUUSD",))
    assert "Нет открытой позиции" in last_text(captured)


def test_why_command_position_without_context(monkeypatch, tmp_path):
    bot, captured = make_bot(monkeypatch)
    monkeypatch.setattr(sc, "LIVE_POSITIONS_PATH", write_contexts(tmp_path, {}))
    monkeypatch.setattr(sc, "_mt5", NS(
        terminal_info=lambda: {"connected": True},
        positions_get=lambda symbol=None, **kw: [make_position(ticket=555)],
    ))
    bot._dispatch("/why", "4242", ("XAUUSD",))
    assert "недоступен" in last_text(captured)


def test_why_command_unknown_asset(monkeypatch):
    bot, captured = make_bot(monkeypatch)
    bot._dispatch("/why", "4242", ("DOGEUSD",))
    assert "Неизвестный актив" in last_text(captured)


def test_metrics_command_today_end_to_end(monkeypatch, tmp_path):
    bot, captured = make_bot(monkeypatch)
    monkeypatch.setattr(sc, "LIVE_POSITIONS_PATH", write_contexts(tmp_path, {}))
    deals = _metrics_deals()
    # Widen the period window so the test does not depend on the wall clock
    # (period_range itself is unit-tested separately).
    wide = lambda kind, now=None: (datetime.now(timezone.utc) - timedelta(days=7),
                                   datetime.now(timezone.utc), "сегодня (UTC)")
    monkeypatch.setattr(sc, "period_range", wide)
    monkeypatch.setattr(sc, "_mt5", NS(
        terminal_info=lambda: {"connected": True},
        history_deals_get=lambda *a, **k: deals,
    ))
    bot._handle_update({"message": {"chat": {"id": 4242}, "text": "/metrics today"}})
    msg = last_text(captured)
    assert "сегодня (UTC)" in msg
    assert "WR 66.7%" in msg and "PF 2.75" in msg and "+35.00" in msg


def test_metrics_command_bad_period(monkeypatch):
    bot, captured = make_bot(monkeypatch)
    bot._dispatch("/metrics", "4242", ("year",))
    assert "Использование" in last_text(captured)


def test_realized_pnl_today_sums_all_deals(monkeypatch):
    """Deterministic, fixed-clock check: realized = profit+swap+commission
    over every deal of the current UTC day, outside-day deals excluded."""
    fixed_now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    deals = [
        make_deal(entry=1, profit=-50.0, swap=-5.0, commission=0.0,
                  time=int((fixed_now - timedelta(hours=2)).timestamp())),   # -55 today
        make_deal(entry=1, profit=10.0, swap=0.0, commission=-1.0,
                  time=int((fixed_now - timedelta(hours=1)).timestamp())),   # +9 today
        make_deal(entry=1, profit=500.0,
                  time=int((fixed_now - timedelta(days=2)).timestamp())),    # other day
    ]
    monkeypatch.setattr(sc, "_mt5", NS(history_deals_get=lambda *a, **k: deals))
    assert sc.realized_pnl_today(now=fixed_now) == pytest.approx(-46.0)


def test_account_command_end_to_end(monkeypatch):
    bot, captured = make_bot(monkeypatch)
    # realized_pnl_today is unit-tested above with a fixed clock; stub it here
    # so the handler test does not depend on the wall clock.
    monkeypatch.setattr(sc, "realized_pnl_today", lambda now=None: -55.0)
    monkeypatch.setattr(sc, "_mt5", NS(
        terminal_info=lambda: {"connected": True},
        account_info=lambda: NS(balance=10000.0, equity=9900.0, margin=100.0,
                                margin_free=9800.0, margin_level=9900.0, profit=-100.0),
    ))
    bot._dispatch("/account", "4242", ())
    msg = last_text(captured)
    assert "10,000.00" in msg      # balance
    assert "-100.00" in msg        # floating = equity - balance
    assert "-55.00" in msg         # realized today


def test_handler_exception_is_reported_not_raised(monkeypatch):
    bot, captured = make_bot(monkeypatch)

    def boom(**kw):
        raise RuntimeError("simulated MT5 IPC failure")

    monkeypatch.setattr(sc, "_mt5", NS(
        terminal_info=lambda: {"connected": True},
        positions_get=boom,
    ))
    bot._dispatch("/status", "4242", ())  # must NOT raise
    assert "Status error" in last_text(captured)


def test_unknown_command(monkeypatch):
    bot, captured = make_bot(monkeypatch)
    bot._dispatch("/bogus", "4242", ())
    assert "Unknown command" in last_text(captured)


# ---------------------------------------------------------------------------
# Safety guard (ТЗ security constraint #1): the status module is read-only.
# A reviewer greps the file for mutating MT5 calls — pin that it stays clean.
# ---------------------------------------------------------------------------

def test_status_module_is_read_only():
    path = os.path.join(os.path.dirname(sc.__file__), "status_commands.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    for forbidden in ("order_send(", "order_close(", "order_check(",
                      "TRADE_ACTION_DEAL", "TRADE_ACTION_SLTP",
                      "position_close", "Close("):
        assert forbidden not in src, f"mutating call {forbidden!r} found in status_commands.py"
