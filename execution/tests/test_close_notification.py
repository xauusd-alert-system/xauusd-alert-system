"""Regression tests for the Telegram CLOSE-notification bug.

Root cause being guarded against: check_and_move_breakeven() used to hit
`if not positions: ...; return` whenever the LAST tracked position disappeared
(the real mt5.positions_get() returns an empty tuple, which is falsy), so the
close detector below never ran: no Telegram "TRADE CLOSED" message, no
log_trade_close on the executed_trades DB, no loss-streak update. Entry
notifications kept working because they are sent synchronously in
execute_signal(), which masked the bug in production.
"""
import types

import pytest

from execution import mt5_trader as trader_mod
from execution.mt5_trader import MultiAssetMT5Trader


class _FakeBot:
    """Captures Telegram messages instead of sending them."""

    def __init__(self):
        self.messages = []

    def send_text_message(self, text):
        self.messages.append(text)
        return True


class _StubThrottle:
    """TradeThrottle stand-in: records closes without halting."""

    def __init__(self):
        self.closed_pnls = []

    def on_trade_closed(self, pnl):
        self.closed_pnls.append(pnl)


def _trader(tmp_path, active):
    t = object.__new__(MultiAssetMT5Trader)
    t.magic_number = 777111
    t.active_trades = dict(active)
    t.be_state = {k for k in active}  # skip per-position SL/TP management logic
    t.streak_losses = {}
    t.signal_features = {}
    t.last_close_pnl = {}
    t.bot = _FakeBot()
    # Attributes added to the trader after this harness was written.
    t.cfg = {"assets": {}}
    t.strategy_identity = "test-identity"
    t.trade_throttle = _StubThrottle()
    t.management_state_path = str(tmp_path / "mgmt_state.json")
    t.trade_db_path = str(tmp_path / "trades.sqlite")
    return t


TRACKED_LONG = {
    "symbol": "XAUUSD", "type": "long", "entry_price": 2000.0,
    "original_volume": 0.10, "tp1": 2005.0, "tp2": 2010.0, "tp3": 2015.0,
    "tp1_hit": True, "tp2_hit": False,
}


def _deal(profit, entry=1, price=2010.0, ts=1_700_000_000):
    return types.SimpleNamespace(
        profit=profit, swap=-1.0, commission=-1.0, entry=entry, time=ts, price=price,
    )


def _stub_open_position(ticket):
    return types.SimpleNamespace(
        ticket=ticket, symbol="EURUSD", type=0, price_open=1.1000, volume=0.10,
    )


def _run_be_check(monkeypatch, trader, positions, deals):
    """Drive check_and_move_breakeven() with a fake MT5 world."""
    monkeypatch.setattr(trader_mod.mt5, "initialize", lambda *a, **k: True)
    monkeypatch.setattr(
        trader_mod, "positions_get_by_magic", lambda *a, **k: positions,
    )
    monkeypatch.setattr(
        trader_mod.mt5, "history_deals_get", lambda *a, **k: deals,
    )
    closed_logged = []
    monkeypatch.setattr(
        trader_mod, "log_trade_close",
        lambda db, ticket, close_time, close_price, pnl: closed_logged.append(
            (ticket, close_price, pnl)
        ),
    )
    purged = []
    monkeypatch.setattr(
        trader_mod, "purge_closed_position_context",
        lambda ticket, path=None: purged.append(ticket),
    )
    trader.check_and_move_breakeven()
    return closed_logged, purged


def test_close_of_last_position_sends_telegram_notification(monkeypatch, tmp_path):
    """The exact production scenario: the only open position hits TP/SL at the
    broker, positions_get() returns () and the close MUST still be reported."""
    t = _trader(tmp_path, {123: dict(TRACKED_LONG)})

    closed_logged, purged = _run_be_check(
        monkeypatch, t,
        positions=[],  # real API empty tuple equivalent
        deals=[_deal(0.0, entry=0, price=2000.0), _deal(52.0, entry=1, price=2010.0)],
    )

    # Telegram close notification was sent (the reported bug: it was not).
    assert len(t.bot.messages) == 1, f"close notification missing: {t.bot.messages}"
    msg = t.bot.messages[0]
    assert "TRADE CLOSED #123" in msg
    assert "XAUUSD" in msg
    assert "+48.00" in msg  # (0-1-1) in-deal + (52-1-1) out-deal = 48

    # The close was persisted for ML retraining and the context purged.
    assert closed_logged == [(123, 2010.0, 48.0)]
    assert purged == [123]
    assert t.active_trades == {}
    assert t.streak_losses.get("XAUUSD") == 0  # profit resets the streak


def test_close_of_last_losing_position_tracks_streak(monkeypatch, tmp_path):
    t = _trader(tmp_path, {456: dict(TRACKED_LONG)})

    _run_be_check(
        monkeypatch, t,
        positions=[],
        deals=[_deal(0.0, entry=0, price=2000.0), _deal(-40.0, entry=1, price=1990.0)],
    )

    assert len(t.bot.messages) == 1
    assert "LOSS/BREAKEVEN" in t.bot.messages[0]
    assert t.streak_losses["XAUUSD"] == 1


def test_position_api_error_keeps_state_and_sends_nothing(monkeypatch, tmp_path):
    """positions_get() == None means an MT5 API error, NOT 'everything closed':
    management state must survive and no (bogus) close message may go out."""
    t = _trader(tmp_path, {123: dict(TRACKED_LONG)})

    closed_logged, purged = _run_be_check(
        monkeypatch, t, positions=None, deals=[],
    )

    assert t.bot.messages == []
    assert closed_logged == []
    assert purged == []
    assert 123 in t.active_trades


def test_close_of_one_position_among_several_still_reported(monkeypatch, tmp_path):
    """Regression guard for the path that already worked: one of two tracked
    positions closes while the other stays open."""
    t = _trader(tmp_path, {123: dict(TRACKED_LONG), 789: dict(TRACKED_LONG)})

    closed_logged, purged = _run_be_check(
        monkeypatch, t,
        positions=[_stub_open_position(789)],  # 123 disappeared -> closed
        deals=[_deal(0.0, entry=0, price=2000.0), _deal(-12.5, entry=1, price=1995.0)],
    )

    assert [123] == [c[0] for c in closed_logged]
    assert purged == [123]
    assert len(t.bot.messages) == 1
    assert "#123" in t.bot.messages[0]
    assert 789 in t.active_trades  # the still-open position is untouched


def test_group_position_counts_three_legs_are_one_group(tmp_path):
    """Audit 2026-08-19: the risk budget mapping counts a 3-leg group as ONE
    slot; tickets unknown to active_trades (restart edge) fall back to single
    positions via the position symbol."""
    t = _trader(tmp_path, {
        1: dict(TRACKED_LONG, leg=1, group_key="G1"),
        2: dict(TRACKED_LONG, leg=2, group_key="G1"),
        3: dict(TRACKED_LONG, leg=3, group_key="G1"),
    })
    positions = [
        types.SimpleNamespace(ticket=1, symbol="GOLD"),
        types.SimpleNamespace(ticket=2, symbol="GOLD"),
        types.SimpleNamespace(ticket=3, symbol="GOLD"),
        types.SimpleNamespace(ticket=99, symbol="EURUSD"),  # unknown to state
    ]

    groups, singles = t._group_position_counts(positions)

    assert groups == {"XAUUSD": {"G1"}}
    assert singles == {"EURUSD": 1}


def test_history_lookup_failure_does_not_suppress_notification(monkeypatch, tmp_path):
    """A failing history_deals_get for one ticket must not take down the close
    handling (and notification) of the other tickets."""

    def boom(*a, **k):
        raise TypeError("position must be int")

    t = _trader(tmp_path, {111: dict(TRACKED_LONG), 222: dict(TRACKED_LONG)})
    monkeypatch.setattr(trader_mod.mt5, "initialize", lambda *a, **k: True)
    monkeypatch.setattr(trader_mod, "positions_get_by_magic", lambda *a, **k: [])
    monkeypatch.setattr(
        trader_mod.mt5, "history_deals_get",
        lambda position=None: boom() if position == 111 else [],
    )
    monkeypatch.setattr(trader_mod, "log_trade_close", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod, "purge_closed_position_context", lambda *a, **k: None)

    t.check_and_move_breakeven()

    assert len(t.bot.messages) == 2  # both closes were reported
    assert t.active_trades == {}
