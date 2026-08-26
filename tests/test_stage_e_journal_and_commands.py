# -*- coding: utf-8 -*-
"""Stage E: /us_* commands with mandatory inline confirmation (ТЗ §12.16),
SQLite journal round-trip, CSV export, runner<->journal integration."""
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from alerts.us_commands import CONFIRM_TTL_SECONDS, UsCommandsController
from usstocks.journal import UsJournal
from usstocks.models import RiskState, TradeSignal
from tests.fixtures.vwap_scenarios import long_scenario

NY = ZoneInfo("America/New_York")


class FakeTransport:
    def __init__(self):
        self.messages = []            # (chat_id, text, reply_markup)
        self.answered = []
        self.documents = []

    def send(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def answer_callback(self, callback_query_id):
        self.answered.append(callback_query_id)

    def send_document(self, chat_id, path):
        self.documents.append((chat_id, path))

    def last_text(self):
        return self.messages[-1][1] if self.messages else ""


@pytest.fixture
def journal(tmp_path):
    j = UsJournal(str(tmp_path / "us.sqlite"))
    yield j
    j.close()


@pytest.fixture
def env(journal):
    state = RiskState(session_date="2026-08-26")
    transport = FakeTransport()
    ctrl = UsCommandsController(journal=journal, state=state,
                                admin_id="111", transport=transport,
                                clock=lambda: 1000.0)
    return {"ctrl": ctrl, "state": state, "transport": transport,
            "journal": journal}


def _signal(symbol="AMD", risk=10.0) -> TradeSignal:
    from datetime import timedelta
    from tests.fixtures.vwap_scenarios import benchmark_uptrend
    bars = long_scenario()
    from usstocks.strategy.vwap_pullback import StrategyConfig, evaluate
    ev = evaluate(symbol, bars, benchmark_uptrend(), side="long",
                  in_watchlist=True, cfg=StrategyConfig(),
                  asof=bars[-1].ts + timedelta(minutes=5),
                  risk_per_trade_usd=risk)
    assert ev.ok, ev.failed
    return ev.signal


def _seed_signal(env, symbol="AMD"):
    sig = _signal(symbol)
    env["journal"].ensure_session("2026-08-26")
    env["journal"].save_signal(sig, session_date="2026-08-26")
    env["state"].active_symbol = symbol
    return sig


# ---------------------------------------------------------------------------
# ТЗ §12.16: P&L changes ONLY after inline confirmation.
# ---------------------------------------------------------------------------

def test_win_requires_confirmation_before_state_changes(env):
    _seed_signal(env)
    env["ctrl"].handle_command("/us_win", "111", ("12.5",))
    assert env["state"].realized_pnl_usd == 0.0        # nothing applied yet
    chat, text, markup = env["transport"].messages[-1]
    assert "Подтвердите" in text and "+12.50$" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "us:confirm"


def test_confirm_applies_win_and_links_outcome_to_signal(env):
    sig = _seed_signal(env)
    env["ctrl"].handle_command("/us_win", "111", ("12.5",))
    env["ctrl"].handle_callback("us:confirm", "111")
    s = env["state"]
    assert s.realized_pnl_usd == pytest.approx(12.5)
    assert s.trades_taken == 1 and s.consecutive_losses == 0
    assert s.active_symbol is None                     # position closed by win
    row = env["journal"].latest_signal(symbol="AMD", decision="taken")
    assert row["signal_id"] == sig.signal_id


def test_loss_increments_consecutive_losses(env):
    _seed_signal(env)
    env["ctrl"].handle_command("/us_loss", "111", ("9.9",))
    env["ctrl"].handle_callback("us:confirm", "111")
    assert env["state"].realized_pnl_usd == pytest.approx(-9.9)
    assert env["state"].consecutive_losses == 1
    outcome = env["journal"]._conn.execute(
        "SELECT outcome FROM us_trade_outcomes").fetchone()
    assert outcome["outcome"] == "loss"


def test_cancel_discards_pending_action(env):
    env["ctrl"].handle_command("/us_pnl", "111", ("5",))
    env["ctrl"].handle_callback("us:cancel", "111")
    assert env["state"].realized_pnl_usd == 0.0
    assert env["state"].trades_taken == 0


def test_expired_confirmation_is_rejected(env):
    env["ctrl"].handle_command("/us_pnl", "111", ("5",))
    env["ctrl"].clock = lambda: 1000.0 + CONFIRM_TTL_SECONDS + 1
    env["ctrl"].handle_callback("us:confirm", "111")
    assert env["state"].realized_pnl_usd == 0.0
    assert "истекло" in env["transport"].last_text()


def test_r_multiple_recorded_from_planned_risk(env):
    sig = _seed_signal(env)
    env["ctrl"].handle_command("/us_win", "111", ("20",))
    env["ctrl"].handle_callback("us:confirm", "111")
    row = env["journal"]._conn.execute(
        "SELECT r_multiple FROM us_trade_outcomes").fetchone()
    # journal rounds to 3 decimals, signal risk ~9.88 -> widen abs tolerance
    assert row["r_multiple"] == pytest.approx(
        round(20 / sig.planned_risk_usd, 3), abs=1e-6)


# ---------------------------------------------------------------------------
# stop-day / flat / signals toggle / admin guard
# ---------------------------------------------------------------------------

def test_stop_and_resume_flow(env):
    env["ctrl"].handle_command("/us_stop", "111", ())
    assert not env["state"].day_stopped                 # not yet!
    env["ctrl"].handle_callback("us:confirm", "111")
    assert env["state"].day_stopped
    env["ctrl"].handle_command("/us_resume", "111", ())
    env["ctrl"].handle_callback("us:confirm", "111")
    assert not env["state"].day_stopped


def test_flat_clears_active_symbol_after_confirm(env):
    env["state"].active_symbol = "AMD"
    env["ctrl"].handle_command("/us_flat", "111", ())
    env["ctrl"].handle_callback("us:confirm", "111")
    assert env["state"].active_symbol is None


def test_non_admin_is_denied_fail_closed(env):
    env["ctrl"].handle_command("/us_pnl", "999", ("100",))
    env["ctrl"].handle_callback("us:confirm", "999")
    assert all(m[0] != "999" or "владельца" in m[1] or "Нет действия" in m[1]
               for m in env["transport"].messages)
    assert env["state"].realized_pnl_usd == 0.0


def test_signals_toggle_direct_no_confirmation_needed(env):
    env["ctrl"].handle_command("/us_signals", "111", ("off",))
    assert env["ctrl"].signals_enabled is False
    env["ctrl"].handle_command("/us_signals", "111", ("on",))
    assert env["ctrl"].signals_enabled is True


def test_unknown_us_command_lists_known(env):
    env["ctrl"].handle_command("/us_foo", "111", ())
    assert "us_status" in env["transport"].last_text()


def test_export_sends_document_or_path(env):
    _seed_signal(env)
    env["ctrl"].handle_command("/us_export", "111", ("2026-08-26",))
    assert env["transport"].documents or "Экспорт" in env["transport"].last_text()


# ---------------------------------------------------------------------------
# Journal round-trip + integration with the risk engine
# ---------------------------------------------------------------------------

def test_journal_schema_tables_exist(journal):
    names = {r[0] for r in journal._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"us_sessions", "us_watchlist_snapshots", "us_signals",
            "us_trade_outcomes", "us_risk_events"} <= names


def test_two_confirmed_losses_block_next_scan(env):
    """Manual outcomes feed the live RiskState: two losses in a row must
    make the very next scan deny MAX_CONSECUTIVE_LOSSES."""
    from usstocks.risk_engine import RiskEngine
    from usstocks.scanner_loop import SignalOnlyRunner

    class FakeProvider:
        def get_bars(self, symbol, count):
            return long_scenario()

    class SilentNotifier:
        signals = []

        def send_signal(self, s):
            pass

        def send_risk_event(self, e):
            pass

        def send_watchlist(self, w):
            pass

    cfg = {"risk": {"max_trades_per_day": 10},  # high enough so consecutive gate fires first
           "challenge": {}, "strategy": {},
           "us_stocks": {"tech_symbols": ["AMD"]}, "session": {"holidays": []}}
    runner = SignalOnlyRunner(cfg, FakeProvider(), SilentNotifier(),
                              watchlist=["AMD"], state=env["state"],
                              journal=env["journal"],
                              symbol_ids={"AMD": "S", "QQQ": "Q"})
    now = datetime(2026, 8, 26, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    for _ in range(2):                       # operator confirms two losses
        env["ctrl"].handle_command("/us_loss", "111", ("8",))
        env["ctrl"].handle_callback("us:confirm", "111")
    assert env["state"].consecutive_losses == 2
    sigs = runner.scan_once(now)
    assert sigs == []                        # engine now denies every entry
    codes = [r["code"] for r in env["journal"]._conn.execute(
        "SELECT code FROM us_risk_events")]
    assert "MAX_CONSECUTIVE_LOSSES" in codes


def test_runner_persists_sent_signal_to_journal():
    from usstocks.risk_engine import RiskEngine
    from usstocks.scanner_loop import SignalOnlyRunner

    class FakeProvider:
        def get_bars(self, symbol, count):
            return long_scenario()

    class CaptureNotifier:
        def __init__(self):
            self.signals = []

        def send_signal(self, s):
            self.signals.append(s)

        def send_risk_event(self, e):
            pass

        def send_watchlist(self, w):
            pass

    notifier = CaptureNotifier()
    cfg = {"risk": {}, "challenge": {}, "strategy": {},
           "us_stocks": {"tech_symbols": ["AMD"]}, "session": {"holidays": []}}
    tmp_db = UsJournal("data/:memory:") if False else None
    # use file-backed temp journal via fixture-less construction
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        jr = UsJournal(td + "/j.sqlite")
        state = RiskState(session_date="2026-08-26")
        runner = SignalOnlyRunner(cfg, FakeProvider(), notifier,
                                  watchlist=["AMD"], state=state,
                                  journal=jr,
                                  symbol_ids={"AMD": "S", "QQQ": "Q"})
        runner.scan_once(datetime(2026, 8, 26, 11, 30, tzinfo=NY))
        rows = jr._conn.execute("SELECT * FROM us_signals").fetchall()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AMD"
        assert rows[0]["decision"] == "pending"
        assert rows[0]["planned_risk_usd"] <= 10
        jr.close()


def test_signal_message_contains_mandatory_fields():
    sig = _signal()
    from usstocks.notify import format_signal_message
    msg = format_signal_message(sig)
    for token in ("Entry", "Stop", "Size", "Notional",
                  "Max risk", "TP1", "TP2",
                  "Signal-only"):
        assert token in msg, f"missing {token} in signal message"
