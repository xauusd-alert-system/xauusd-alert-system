"""Unit tests for the breakeven / position-management block of
MultiAssetMT5Trader (Phase 5, step 2e).

Scope (from the term-missing report of step 2d):

    check_and_move_breakeven   137 uncovered lines — the last large block
    _move_sl_to_entry           10 uncovered lines
    execute_signal              12 uncovered lines: the injected-exception
                                handlers listed at the end of this module

Deliberately out of scope: run_loop(), __init__(), main().

The trader is built with ``object.__new__`` and only the attributes the code
path under test reads are assigned (same pattern as steps 2c/2d) — ``__init__``
loads config, starts the book feed and builds ML pipelines, none of which these
paths touch.

MT5 access goes through mt5_adapter.testing.MockMT5Module. NOTE: mt5_trader
resolves ``mt5 = get_mt5_module()`` at IMPORT time, so patching
``mt5_adapter.lazy.get_mt5_module`` leaves the module handle untouched — the
``mt5`` attribute on the trader module itself must be swapped.

Every side channel that would touch disk or the network is replaced by a
recording stub, so the tests stay hermetic and can assert on what would have
been persisted.
"""

from __future__ import annotations

import time
import types

import pytest

from execution import mt5_trader as trader_mod
from execution.mt5_trader import MultiAssetMT5Trader
from mt5_adapter.testing import (
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_SLTP,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_REJECT,
    MockMT5Module,
    _OrderResultTuple,
)

TRADER_LOG = "multi_asset_trader"

MAGIC = 777111
SYMBOL = "GOLD"
ASSET = "XAUUSD"

# GOLD: point 0.01, digits 2, stops 10 pts, freeze 5 pts, spread 30 pts
# -> _get_min_dist = max(0.10, 0.05, 0.30 + 30 * 0.01) = 0.60
# -> _be_min_dist  = max(10, 5) * 0.01 = 0.10
BID = 2400.00
ASK = 2400.30

ENTRY = 2400.00
RETCODE_MARKET_CLOSED = 10018


# ---------------------------------------------------------------------------
# Recording stubs
# ---------------------------------------------------------------------------


class _Bot:
    def __init__(self):
        self.messages = []
        self.alerts = []

    def send_text_message(self, text):
        self.messages.append(text)
        return True

    def send_alert_if_qualified(self, signal, asset_key):
        self.alerts.append((asset_key, signal))
        return True


class _Throttle:
    def __init__(self, ok=True, reason="", risk_multiplier=1.0):
        self.ok = ok
        self.reason = reason
        self._mult = risk_multiplier
        self.closed_pnls = []
        self.equities_seen = []

    def can_trade(self, equity):
        self.equities_seen.append(equity)
        return self.ok, self.reason

    def risk_multiplier(self):
        return self._mult

    def on_trade_closed(self, pnl):
        self.closed_pnls.append(pnl)


class _Recorder:
    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return None

    @property
    def kwargs(self):
        return [k for _, k in self.calls]

    @property
    def count(self):
        return len(self.calls)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg(**blackout_overrides):
    bo = {"enabled": False}
    bo.update(blackout_overrides)
    return {
        "assets": {
            ASSET: {
                "enabled": True,
                "mt5_symbol": SYMBOL,
                "ensemble": {"min_confidence_to_alert": 0.60},
            }
        },
        "ensemble": {"min_confidence_to_alert": 0.60},
        "execution": {"trading_blackout": bo},
        "market_data": {"timeframe": "M5"},
    }


SIGNAL = {
    "bias": "long",
    "confidence": 0.90,
    "regime": "trend_up",
    "step": 1.0,
    "signal_id": "sig-1",
    "timestamp_utc": 1_700_000_000,
    "features": {"atr": 1.5},
}


def _signal(**overrides):
    sig = dict(SIGNAL)
    sig.update(overrides)
    return sig


@pytest.fixture()
def mock_mt5(monkeypatch):
    mock = MockMT5Module()  # trade_mode defaults to ACCOUNT_TRADE_MODE_DEMO
    mock.initialize()  # account_info()/positions_get() return None until then
    mock.set_symbol_info(SYMBOL, digits=2, point=0.01, trade_stops_level=10, trade_freeze_level=5)
    mock.set_tick(SYMBOL, bid=BID, ask=ASK)

    def positions_get_by_magic(symbol=None, magic=None):
        found = [p for p in mock.positions if magic is None or p.magic == magic]
        if symbol is not None:
            found = [p for p in found if p.symbol == symbol]
        return found

    monkeypatch.setattr(trader_mod, "positions_get_by_magic", positions_get_by_magic)
    monkeypatch.setattr(trader_mod, "mt5", mock)
    # Deterministic fills: the bare mock returns its internal next-ticket
    # counter. Tests that need another behaviour install their own handler.
    mock.order_send_handler = lambda request: _result()
    return mock


@pytest.fixture()
def sinks(monkeypatch):
    """Patch every disk/network side channel with a recorder."""
    rec = types.SimpleNamespace(
        intent=_Recorder(),
        ledger=_Recorder(),  # log_execution_attempt
        entry=_Recorder(),  # log_trade_entry
        context=_Recorder(),  # record_position_context
        events=_Recorder(),  # append_trading_event
        outbox=_Recorder(),  # enqueue_event
        validate=_Recorder(),
        close=_Recorder(),  # log_trade_close
        purge=_Recorder(),  # purge_closed_position_context
    )
    routing = {"allowed": True, "reason": "live_systematic"}
    grid = {"grid": {"tp1_mult": 1.0, "tp2_mult": 2.0, "tp3_mult": 3.0, "stop_mult": 2.0}}

    monkeypatch.setattr(trader_mod, "append_signal_intent", rec.intent)
    monkeypatch.setattr(trader_mod, "log_execution_attempt", rec.ledger)
    monkeypatch.setattr(trader_mod, "log_trade_entry", rec.entry)
    monkeypatch.setattr(trader_mod, "record_position_context", rec.context)
    monkeypatch.setattr(trader_mod, "append_trading_event", rec.events)
    monkeypatch.setattr(trader_mod, "enqueue_event", rec.outbox)
    monkeypatch.setattr(trader_mod, "validate_symbol", rec.validate)
    monkeypatch.setattr(trader_mod, "log_trade_close", rec.close)
    monkeypatch.setattr(trader_mod, "purge_closed_position_context", rec.purge)
    monkeypatch.setattr(
        trader_mod, "order_routing_allowed", lambda cfg, confirmed_by=None: (routing["allowed"], routing["reason"])
    )
    monkeypatch.setattr(trader_mod, "get_signal_grid", lambda cfg, asset_cfg, regime="": grid["grid"])

    rec.routing = routing
    rec.grid = grid
    return rec


def _result(retcode=TRADE_RETCODE_DONE, order=555, deal=666, volume=0.03, price=ASK, comment=""):
    return _OrderResultTuple(
        retcode=retcode,
        deal=deal,
        order=order,
        volume=volume,
        price=price,
        comment=comment,
        request_id=order,
        retcode_external=0,
    )


def _reject(comment="rejected", retcode=TRADE_RETCODE_REJECT):
    return _result(retcode=retcode, order=0, deal=0, volume=0.0, price=0.0, comment=comment)


def _deal(profit=0.0, price=ENTRY, entry=1, time=1_700_000_500, swap=0.0, commission=0.0):
    return types.SimpleNamespace(
        profit=profit,
        swap=swap,
        commission=commission,
        entry=entry,
        price=price,
        time=time,
    )


# ---------------------------------------------------------------------------
# Trader builders
# ---------------------------------------------------------------------------


def _be_trader(trades=None, *, signals=None, **overrides):
    """Trader wired for check_and_move_breakeven() / _move_sl_to_entry()."""
    t = object.__new__(MultiAssetMT5Trader)
    t.magic_number = MAGIC
    t.active_trades = trades if trades is not None else {}
    t.signal_features = signals if signals is not None else {}
    t.be_state = {}
    t.be_trigger_by_symbol = {}
    t.trailing_atr_mult_by_symbol = {}
    t.streak_losses = {}
    t.last_close_pnl = {}
    t.bot = _Bot()
    t.trade_throttle = _Throttle()
    t.trade_db_path = "test-only.db"
    t.cfg = {"assets": {}}
    t.dry_run = False
    t.strategy_identity = {"strategy_version": "sv-1", "config_hash": "cfg-hash-1"}
    t.saves = []
    t._save_management_state = lambda: t.saves.append(1)
    # Profit trailing needs config + live ATR; both are step 2c/other-scope
    # concerns, so they default to "disabled" and are overridden per test.
    t._get_profit_trail_config = lambda symbol: None
    t._latest_causal_atr = lambda asset_key: 0.0
    for name, value in overrides.items():
        setattr(t, name, value)
    return t


def _sig_trader(cfg=None, *, volume=0.09, dry_run=False, **overrides):
    """Trader wired for execute_signal() (the injected-exception branches)."""
    t = object.__new__(MultiAssetMT5Trader)
    t.cfg = cfg if cfg is not None else _cfg()
    t.magic_number = MAGIC
    t.volume = volume
    t.dry_run = dry_run
    t.bot = _Bot()
    t.risk_manager = types.SimpleNamespace(
        can_trade=lambda *a: (True, ""),
        record_trade_executed=lambda asset_key: None,
    )
    t.trade_throttle = _Throttle()
    t.active_trades = {}
    t.signal_features = {}
    t.streak_losses = {}
    t.execution_assets = {ASSET}
    t.order_routing_enabled = True
    t.strategy_identity = {"strategy_version": "sv-1", "config_hash": "cfg-hash-1"}
    t.deployment_mode = types.SimpleNamespace(value="demo_systematic")
    t.trade_db_path = "test-only.db"
    # Correlation filter defaults to ENABLED when absent — switch it off.
    t.corr_filter_cfg = {"enabled": False}
    t.corr_threshold = 0.80
    t.corr_matrix = {}
    t.corr_history_bars = 500
    t.corr_update_interval = 60
    t.corr_last_update = time.time()
    t._init_blackout()
    t.saves = []
    t._save_management_state = lambda: t.saves.append(1)
    for name, value in overrides.items():
        setattr(t, name, value)
    return t


def _legacy_trade(ticket, *, tp1=None, tp2=None, tp3=None, entry=ENTRY, volume=0.09, tp1_hit=False, tp2_hit=False):
    return {
        "symbol": SYMBOL,
        "type": "long",
        "entry_price": entry,
        "original_volume": volume,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
    }


def _leg_trade(ticket, leg, *, group_key="XAUUSD:sig-1", entry=ENTRY, be_done=False):
    return {
        "symbol": ASSET,
        "type": "long",
        "entry_price": entry,
        "original_volume": 0.03,
        "leg": leg,
        "group_key": group_key,
        "tp1": 2401.30,
        "tp2": None,
        "tp3": None,
        "tp1_hit": False,
        "tp2_hit": False,
        "be_done": be_done,
    }


def _add(mock, ticket, *, type=0, volume=0.09, price_open=ENTRY, sl=2398.00, tp=2403.00, magic=MAGIC):
    return mock.add_position(
        SYMBOL, ticket=ticket, type=type, volume=volume, price_open=price_open, sl=sl, tp=tp, magic=magic
    )


def _sent(mock, action=None):
    reqs = [c[1][0] for c in mock.calls if c[0] == "order_send"]
    if action is None:
        return reqs
    return [r for r in reqs if r["action"] == action]


# ---------------------------------------------------------------------------
# _move_sl_to_entry
# ---------------------------------------------------------------------------


def test_move_sl_to_entry_reaches_a_true_breakeven(mock_mt5, sinks):
    t = _be_trader()
    pos = _add(mock_mt5, 7001, sl=2398.00)
    # anchor + 1 tick = 2400.01; broker minimum = ask 2400.30 - 0.10 = 2400.20
    # -> 2400.01 <= 2400.20, so no clamp and the true breakeven is reachable.
    assert t._move_sl_to_entry(pos, ENTRY) is True

    req = _sent(mock_mt5, TRADE_ACTION_SLTP)[0]
    assert req["position"] == 7001
    assert req["symbol"] == SYMBOL
    assert req["sl"] == pytest.approx(2400.01)
    assert req["tp"] == pytest.approx(2403.00)


def test_move_sl_to_entry_clamps_to_the_broker_minimum(mock_mt5, sinks):
    """A tight market (large stops level) makes entry unreachable: the SL is
    still pulled as close as allowed, but the move reports False so the caller
    keeps retrying."""
    mock_mt5.set_symbol_info(SYMBOL, digits=2, point=0.01, trade_stops_level=50, trade_freeze_level=0)
    t = _be_trader()
    pos = _add(mock_mt5, 7002, sl=2398.00)
    # be_dist = 50 * 0.01 = 0.50 -> min_sl = ask 2400.30 - 0.50 = 2399.80
    assert t._move_sl_to_entry(pos, ENTRY) is False

    req = _sent(mock_mt5, TRADE_ACTION_SLTP)[0]
    assert req["sl"] == pytest.approx(2399.80)  # improved, but not yet breakeven


def test_move_sl_to_entry_mirrors_for_a_short(mock_mt5, sinks):
    t = _be_trader()
    pos = _add(mock_mt5, 7003, type=1, sl=2402.00)
    # The distance is measured from the fill side: the BID for a short.
    # bid 2399.80 + be_dist 0.10 = 2399.90 <= target 2399.99 -> no clamp.
    mock_mt5.set_tick(SYMBOL, bid=2399.80, ask=2400.10)
    assert t._move_sl_to_entry(pos, ENTRY) is True
    assert _sent(mock_mt5, TRADE_ACTION_SLTP)[0]["sl"] == pytest.approx(2399.99)


def test_move_sl_to_entry_is_idempotent_once_the_sl_is_already_better(mock_mt5, sinks):
    """An SL already at or beyond the breakeven target must not be pushed back
    out — that would hand back locked-in profit."""
    t = _be_trader()
    pos = _add(mock_mt5, 7004, sl=2401.50)  # already better than 2400.01
    assert t._move_sl_to_entry(pos, ENTRY) is False
    assert _sent(mock_mt5) == []


def test_move_sl_to_entry_short_skips_when_already_better(mock_mt5, sinks):
    t = _be_trader()
    pos = _add(mock_mt5, 7005, type=1, sl=2398.00)  # already below 2399.99
    assert t._move_sl_to_entry(pos, ENTRY) is False
    assert _sent(mock_mt5) == []


def test_move_sl_to_entry_without_market_data(mock_mt5, sinks):
    t = _be_trader()
    mock_mt5.ticks.pop(SYMBOL)
    assert t._move_sl_to_entry(_add(mock_mt5, 7006), ENTRY) is False
    assert _sent(mock_mt5) == []


def test_move_sl_to_entry_swallows_errors(mock_mt5, sinks, caplog):
    t = _be_trader()

    def boom(*a, **k):
        raise RuntimeError("terminal disconnected")

    t._modify_sl_tp = boom
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        assert t._move_sl_to_entry(_add(mock_mt5, 7007), ENTRY) is False
    assert "Breakeven move failed" in caplog.text


def test_move_sl_to_entry_reports_a_broker_rejection(mock_mt5, sinks):
    t = _be_trader()
    mock_mt5.order_send_handler = lambda req: _reject("invalid stops")
    assert t._move_sl_to_entry(_add(mock_mt5, 7008), ENTRY) is False


# ---------------------------------------------------------------------------
# check_and_move_breakeven — entry guards
# ---------------------------------------------------------------------------


def test_be_check_keeps_state_when_positions_get_errors(mock_mt5, sinks, monkeypatch, caplog):
    """mt5.positions_get() returns None ONLY on an API error. The old code
    conflated that with "no positions" and wiped active_trades before the
    close detector ran, silently losing every close notification."""
    trades = {1: _legacy_trade(1)}
    t = _be_trader(trades)
    monkeypatch.setattr(trader_mod, "positions_get_by_magic", lambda **k: None)
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        t.check_and_move_breakeven()

    assert "positions_get failed" in caplog.text
    assert t.active_trades == trades  # state preserved for the next tick
    assert t.saves == []  # and nothing is persisted
    assert t.bot.messages == []


def test_be_check_runs_the_close_detector_when_the_last_position_closed(mock_mt5, sinks):
    """With an EMPTY position list the close detector must still fire — this is
    the regression the None/() distinction above protects."""
    t = _be_trader({1: _legacy_trade(1)})
    t.check_and_move_breakeven()
    assert t.active_trades == {}
    assert len(t.bot.messages) == 1
    assert "TRADE CLOSED #1" in t.bot.messages[0]
    assert sinks.close.count == 1
    assert t.saves  # management state persisted before and after the detector


def test_be_check_bails_out_when_initialize_fails(mock_mt5, sinks):
    t = _be_trader({1: _legacy_trade(1)})
    mock_mt5.initialize = lambda *a, **k: False
    t.check_and_move_breakeven()
    assert t.bot.messages == []
    assert t.saves == []


def test_be_check_registers_an_untracked_position(mock_mt5, sinks):
    """A position the bot does not know about (restart, manual entry) is
    adopted with empty TP targets instead of being ignored."""
    t = _be_trader()
    _add(mock_mt5, 8100, volume=0.12, price_open=2401.00)
    t.check_and_move_breakeven()

    trade = t.active_trades[8100]
    assert trade["symbol"] == SYMBOL
    assert trade["type"] == "long"
    assert trade["entry_price"] == pytest.approx(2401.00)
    assert trade["original_volume"] == pytest.approx(0.12)
    assert trade["tp1"] is None
    assert trade["tp1_hit"] is False
    assert t.signal_features[8100]["entry_price"] == pytest.approx(2401.00)


def test_be_check_skips_tickets_marked_in_be_state(mock_mt5, sinks):
    """be_state is the external skip-set: a ticket in it is never managed."""
    t = _be_trader({8200: _legacy_trade(8200, tp1=2401.00)}, be_state={8200: True})
    _add(mock_mt5, 8200, volume=0.09)
    mock_mt5.set_tick(SYMBOL, bid=2402.00, ask=2402.30)
    t.check_and_move_breakeven()
    assert _sent(mock_mt5) == []


def test_be_check_skips_positions_without_market_data(mock_mt5, sinks):
    t = _be_trader({8300: _legacy_trade(8300, tp1=2401.00)})
    _add(mock_mt5, 8300)
    mock_mt5.ticks.pop(SYMBOL)
    t.check_and_move_breakeven()
    assert _sent(mock_mt5) == []


def test_be_check_ignores_positions_of_another_magic(mock_mt5, sinks):
    """Only this system's magic is managed — a foreign position sharing the
    symbol must not be touched."""
    t = _be_trader()
    _add(mock_mt5, 8400, magic=999999, volume=0.50)
    _add(mock_mt5, 8401, magic=MAGIC, volume=0.09)
    t.check_and_move_breakeven()
    # The foreign ticket is never adopted.
    assert set(t.active_trades) == {8401}


# ---------------------------------------------------------------------------
# check_and_move_breakeven — the partial-close ladder
# ---------------------------------------------------------------------------


def test_be_check_moves_sl_to_entry_after_tp1(mock_mt5, sinks):
    """TP1 reached -> close 50% at bid, then pull the SL to (just past) entry.
    Entry + 1 tick = 2400.01 is reachable here because the broker minimum
    (bid 2400.55 - 0.60 = 2399.95) sits below it."""
    t = _be_trader({8500: _legacy_trade(8500, tp1=2400.50, tp2=2402.00, tp3=2403.00, volume=0.09)})
    _add(mock_mt5, 8500, volume=0.09, sl=2398.00, tp=2403.00)
    mock_mt5.set_tick(SYMBOL, bid=2400.55, ask=2400.85)

    t.check_and_move_breakeven()

    closes = _sent(mock_mt5, TRADE_ACTION_DEAL)
    assert len(closes) == 1
    assert closes[0]["volume"] == pytest.approx(0.04)  # 50% of 0.09, floored
    assert closes[0]["price"] == pytest.approx(2400.55)  # a long closes at bid
    assert closes[0]["type"] == ORDER_TYPE_SELL
    assert closes[0]["comment"] == "TP1 (50%) close"

    mods = _sent(mock_mt5, TRADE_ACTION_SLTP)
    assert len(mods) == 1
    assert mods[0]["position"] == 8500
    assert mods[0]["sl"] == pytest.approx(2400.01)  # entry + 1 tick
    assert mods[0]["tp"] == pytest.approx(2403.00)
    assert t.active_trades[8500]["tp1_hit"] is True


def test_be_check_clamps_the_breakeven_sl_to_the_broker_minimum(mock_mt5, sinks):
    """When the market has run far past entry the broker minimum dominates and
    the SL lands at bid - min_dist rather than at entry."""
    t = _be_trader({8600: _legacy_trade(8600, tp1=2401.00, volume=0.09)})
    _add(mock_mt5, 8600, volume=0.09, tp=2403.00)
    mock_mt5.set_tick(SYMBOL, bid=2401.50, ask=2401.80)  # min_sl = 2401.50 - 0.60 = 2400.90

    t.check_and_move_breakeven()
    assert _sent(mock_mt5, TRADE_ACTION_SLTP)[0]["sl"] == pytest.approx(2400.90)


def test_be_check_is_idempotent_on_the_next_tick(mock_mt5, sinks):
    """tp1_hit is the guard: a second pass must not re-close TP1 or re-send the
    SL modify."""
    t = _be_trader({8700: _legacy_trade(8700, tp1=2400.50, tp2=2402.00, volume=0.09)})
    _add(mock_mt5, 8700)
    mock_mt5.set_tick(SYMBOL, bid=2400.55, ask=2400.85)

    t.check_and_move_breakeven()
    first = len(_sent(mock_mt5))
    assert t.active_trades[8700]["tp1_hit"] is True

    t.check_and_move_breakeven()
    assert len(_sent(mock_mt5)) == first  # nothing further was sent
    assert t.active_trades[8700]["tp2_hit"] is False  # TP2 not reached yet


def test_be_check_advances_to_tp2_only_after_tp1(mock_mt5, sinks):
    t = _be_trader({8800: _legacy_trade(8800, tp1=2400.50, tp2=2401.00, volume=0.09, tp1_hit=True)})
    _add(mock_mt5, 8800)
    mock_mt5.set_tick(SYMBOL, bid=2401.20, ask=2401.50)

    t.check_and_move_breakeven()
    closes = _sent(mock_mt5, TRADE_ACTION_DEAL)
    assert len(closes) == 1
    assert closes[0]["comment"] == "TP2 (30%) close"
    assert closes[0]["volume"] == pytest.approx(0.02)  # 30% of 0.09, floored
    assert t.active_trades[8800]["tp2_hit"] is True


def test_be_check_closes_the_remainder_at_tp3(mock_mt5, sinks):
    t = _be_trader({8900: _legacy_trade(8900, tp1=2400.10, tp2=2400.20, tp3=2401.00, tp1_hit=True, tp2_hit=True)})
    _add(mock_mt5, 8900)
    mock_mt5.set_tick(SYMBOL, bid=2401.50, ask=2401.80)

    t.check_and_move_breakeven()
    closes = _sent(mock_mt5, TRADE_ACTION_DEAL)
    assert len(closes) == 1
    assert closes[0]["comment"] == "TP3 (20%) close"
    assert closes[0]["volume"] == pytest.approx(0.09)  # the whole remainder


def test_be_check_does_not_advance_state_when_the_partial_close_is_rejected(mock_mt5, sinks):
    """W12: tp1_hit used to be set before the retcode check, so a rejected
    partial (requote / market closed) was treated as executed forever."""
    mock_mt5.TRADE_RETCODE_MARKET_CLOSED = RETCODE_MARKET_CLOSED
    mock_mt5.order_send_handler = lambda req: _reject("market closed", retcode=RETCODE_MARKET_CLOSED)
    t = _be_trader({9000: _legacy_trade(9000, tp1=2400.50, volume=0.09)})
    _add(mock_mt5, 9000)
    mock_mt5.set_tick(SYMBOL, bid=2400.55, ask=2400.85)

    t.check_and_move_breakeven()
    assert t.active_trades[9000]["tp1_hit"] is False  # retried next tick
    assert _sent(mock_mt5, TRADE_ACTION_SLTP) == []  # no premature breakeven


def test_be_check_skips_a_tranche_below_the_broker_minimum(mock_mt5, sinks):
    """A 0.01 base lot cannot be halved into a fillable tranche: nothing is
    sent and the remainder stays on the broker TP."""
    t = _be_trader({9100: _legacy_trade(9100, tp1=2400.50, volume=0.01)})
    _add(mock_mt5, 9100, volume=0.01)
    mock_mt5.set_tick(SYMBOL, bid=2400.55, ask=2400.85)

    t.check_and_move_breakeven()
    assert _sent(mock_mt5) == []
    assert t.active_trades[9100]["tp1_hit"] is False


def test_be_check_mirrors_the_ladder_for_a_short(mock_mt5, sinks):
    t = _be_trader(
        {
            9200: {
                "symbol": SYMBOL,
                "type": "short",
                "entry_price": ENTRY,
                "original_volume": 0.09,
                "tp1": 2399.50,
                "tp2": 2398.00,
                "tp3": 2397.00,
                "tp1_hit": False,
                "tp2_hit": False,
            }
        }
    )
    _add(mock_mt5, 9200, type=1, sl=2402.00, tp=2397.00)
    # spread kept at 0.30 so _get_min_dist stays 0.60; ask <= tp1
    mock_mt5.set_tick(SYMBOL, bid=2399.00, ask=2399.30)

    t.check_and_move_breakeven()
    closes = _sent(mock_mt5, TRADE_ACTION_DEAL)
    assert len(closes) == 1
    assert closes[0]["type"] == ORDER_TYPE_BUY  # a short is closed by a buy
    assert closes[0]["price"] == pytest.approx(2399.30)  # at ask
    # target = 2399.99; min_sl = ask 2399.30 + 0.60 = 2399.90 -> clamped up
    assert _sent(mock_mt5, TRADE_ACTION_SLTP)[0]["sl"] == pytest.approx(2399.90)


# ---------------------------------------------------------------------------
# check_and_move_breakeven — 3-leg groups
# ---------------------------------------------------------------------------


def test_be_check_moves_legs_2_and_3_to_entry_when_leg_1_closes(mock_mt5, sinks):
    """The 3-leg BE: leg 1 is closed by its broker TP1, so the SL of the
    remaining legs is pulled to the group entry."""
    trades = {
        1: _leg_trade(1, 1),
        2: _leg_trade(2, 2),
        3: _leg_trade(3, 3),
    }
    t = _be_trader(trades)
    # Leg 1 is gone from the broker; legs 2 and 3 remain.
    _add(mock_mt5, 2, volume=0.03, sl=2398.00)
    _add(mock_mt5, 3, volume=0.03, sl=2398.00)

    t.check_and_move_breakeven()

    mods = _sent(mock_mt5, TRADE_ACTION_SLTP)
    assert sorted(r["position"] for r in mods) == [2, 3]
    assert all(r["sl"] == pytest.approx(2400.01) for r in mods)  # entry + 1 tick
    assert t.active_trades[2]["be_done"] is True
    assert t.active_trades[3]["be_done"] is True


def test_be_check_only_moves_the_legs_of_the_closing_group(mock_mt5, sinks):
    """When leg 1 of group A closes, only group A's remaining legs are pulled
    to entry. Group B keeps its own SL — its leg 1 is still open, so neither
    the retry block nor the close detector may touch it."""
    t = _be_trader(
        {
            1: _leg_trade(1, 1, group_key="XAUUSD:sig-A"),
            2: _leg_trade(2, 2, group_key="XAUUSD:sig-A"),
            8: _leg_trade(8, 1, group_key="XAUUSD:sig-B"),  # still open
            9: _leg_trade(9, 2, group_key="XAUUSD:sig-B"),
        }
    )
    _add(mock_mt5, 2, sl=2398.00)
    _add(mock_mt5, 8, sl=2398.00)
    _add(mock_mt5, 9, sl=2398.00)

    t.check_and_move_breakeven()
    mods = _sent(mock_mt5, TRADE_ACTION_SLTP)
    assert [r["position"] for r in mods] == [2]
    assert t.active_trades[9]["be_done"] is False


def test_be_check_does_not_retry_a_leg_that_is_already_done(mock_mt5, sinks):
    """be_done is the idempotency guard for the retry block: once a true
    breakeven was accepted, the SL is not pushed again."""
    t = _be_trader({5: _leg_trade(5, 2, be_done=True)})
    _add(mock_mt5, 5, sl=2400.01)
    t.check_and_move_breakeven()
    assert _sent(mock_mt5, TRADE_ACTION_SLTP) == []


def test_be_check_retries_a_breakeven_that_was_clamped(mock_mt5, sinks):
    """A clamped move returns False and leaves be_done unset, so the next tick
    tightens the SL further as the market drifts in our favour."""
    t = _be_trader({6: _leg_trade(6, 2)})
    _add(mock_mt5, 6, sl=2398.00)
    mock_mt5.set_symbol_info(SYMBOL, digits=2, point=0.01, trade_stops_level=50, trade_freeze_level=0)

    t.check_and_move_breakeven()  # be_dist 0.50 -> min_sl 2399.80 -> clamped
    assert _sent(mock_mt5, TRADE_ACTION_SLTP)[0]["sl"] == pytest.approx(2399.80)
    assert t.active_trades[6]["be_done"] is False

    t.check_and_move_breakeven()  # retried, not skipped
    assert len(_sent(mock_mt5, TRADE_ACTION_SLTP)) == 2


def test_be_check_keeps_retrying_while_leg_1_is_still_open(mock_mt5, sinks):
    """The retry block only fires once leg 1 has left active_trades. While it is
    still tracked, the move is deferred to the close detector."""
    t = _be_trader({1: _leg_trade(1, 1), 2: _leg_trade(2, 2)})
    _add(mock_mt5, 1, sl=2398.00)
    _add(mock_mt5, 2, sl=2398.00)
    t.check_and_move_breakeven()
    assert _sent(mock_mt5, TRADE_ACTION_SLTP) == []


def test_be_check_skips_a_leg_whose_position_is_missing(mock_mt5, sinks):
    """A tracked leg with no matching broker position cannot be modified."""
    t = _be_trader({7: _leg_trade(7, 2)})
    _add(mock_mt5, 7, sl=2398.00)
    mock_mt5.positions = []  # vanished between the tick and the move
    t.check_and_move_breakeven()
    assert _sent(mock_mt5, TRADE_ACTION_SLTP) == []


# ---------------------------------------------------------------------------
# check_and_move_breakeven — early breakeven and profit trailing
# ---------------------------------------------------------------------------


def test_be_check_moves_the_sl_early_with_a_sub_one_trigger(mock_mt5, sinks, caplog):
    """be_trigger < 1.0 pulls the SL to entry BEFORE TP1 (mean-reverting FX
    wants the shorter tail)."""
    t = _be_trader({9500: _legacy_trade(9500, tp1=2402.00, volume=0.09)}, be_trigger_by_symbol={SYMBOL: 0.5})
    _add(mock_mt5, 9500, volume=0.09, tp=2403.00)
    # step_dist = |2402 - 2400| = 2.00; trigger 0.5 -> fires at 2401.00
    mock_mt5.set_tick(SYMBOL, bid=2401.10, ask=2401.40)

    with caplog.at_level("INFO", logger=TRADER_LOG):
        t.check_and_move_breakeven()

    mods = _sent(mock_mt5, TRADE_ACTION_SLTP)
    assert len(mods) == 1
    # target 2400.01; min_sl = 2401.10 - 0.60 = 2400.50 -> clamped
    assert mods[0]["sl"] == pytest.approx(2400.50)
    assert "EARLY BREAKEVEN" in caplog.text
    assert t.active_trades[9500]["be_done"] is True
    assert _sent(mock_mt5, TRADE_ACTION_DEAL) == []  # nothing was closed


def test_be_check_does_not_fire_early_before_the_trigger(mock_mt5, sinks):
    t = _be_trader({9600: _legacy_trade(9600, tp1=2402.00, volume=0.09)}, be_trigger_by_symbol={SYMBOL: 0.5})
    _add(mock_mt5, 9600)
    mock_mt5.set_tick(SYMBOL, bid=2400.50, ask=2400.80)  # below 2401.00
    t.check_and_move_breakeven()
    assert _sent(mock_mt5) == []


def test_be_check_trails_the_stop_into_profit_after_breakeven(mock_mt5, sinks, caplog):
    """After BE, the stop trails and locks in lock_pct of the unrealized
    profit. It never moves back out."""
    t = _be_trader({9700: _legacy_trade(9700, volume=0.09, tp1_hit=True)})
    t.active_trades[9700]["be_done"] = True
    t._get_profit_trail_config = lambda symbol: {"activation_atr": 0.5, "lock_pct": 0.60, "min_profit_price": 0}
    t._latest_causal_atr = lambda asset_key: 1.0
    _add(mock_mt5, 9700, volume=0.09, sl=2400.01, tp=2403.00)
    # unrealized = 2401.00 - 2400.00 = 1.00 > max(0.5 * 1.0, 0) = 0.50
    mock_mt5.set_tick(SYMBOL, bid=2401.00, ask=2401.30)

    with caplog.at_level("INFO", logger=TRADER_LOG):
        t.check_and_move_breakeven()

    mods = _sent(mock_mt5, TRADE_ACTION_SLTP)
    assert len(mods) == 1
    # trail_sl = 2401.00 - 1.00 * (1 - 0.60) = 2400.60; the broker minimum
    # (2401.00 - 0.60 = 2400.40) sits below it, so the formula is used as-is.
    assert mods[0]["sl"] == pytest.approx(2400.60)
    assert "PROFIT TRAIL" in caplog.text
    assert t.active_trades[9700]["trailing_active"] is True


def test_be_check_never_trails_the_stop_back_out(mock_mt5, sinks):
    """A trailing stop that would be below the current SL is not sent."""
    t = _be_trader({9800: _legacy_trade(9800, volume=0.09, tp1_hit=True)})
    t.active_trades[9800]["be_done"] = True
    t._get_profit_trail_config = lambda symbol: {"activation_atr": 0.5, "lock_pct": 0.60, "min_profit_price": 0}
    t._latest_causal_atr = lambda asset_key: 1.0
    _add(mock_mt5, 9800, volume=0.09, sl=2405.00, tp=2403.00)  # SL already tighter
    mock_mt5.set_tick(SYMBOL, bid=2402.00, ask=2402.30)
    t.check_and_move_breakeven()
    assert _sent(mock_mt5, TRADE_ACTION_SLTP) == []


def test_be_check_survives_an_unavailable_causal_atr(mock_mt5, sinks, caplog):
    """A missing live ATR must not break the trailing block."""
    t = _be_trader({9900: _legacy_trade(9900, volume=0.09, tp1_hit=True)})
    t.active_trades[9900]["be_done"] = True
    t._get_profit_trail_config = lambda symbol: {"activation_atr": 0.5, "lock_pct": 0.60, "min_profit_price": 0.20}

    def boom(asset_key):
        raise RuntimeError("no live pipeline")

    t._latest_causal_atr = boom
    _add(mock_mt5, 9900, volume=0.09, sl=2400.01, tp=2403.00)
    mock_mt5.set_tick(SYMBOL, bid=2401.00, ask=2401.30)
    t.check_and_move_breakeven()
    # atr_now falls back to 0, so activation uses min_profit_price 0.20;
    # unrealized 1.00 > 0.20 -> the stop still trails.
    assert _sent(mock_mt5, TRADE_ACTION_SLTP)[0]["sl"] == pytest.approx(2400.60)


# ---------------------------------------------------------------------------
# check_and_move_breakeven — close detector
# ---------------------------------------------------------------------------


def test_close_detector_reports_a_profit_and_resets_the_streak(mock_mt5, sinks):
    # The streak is keyed by the trade's own symbol, which for a legacy
    # single-position trade is the broker symbol.
    t = _be_trader({1: _legacy_trade(1)}, streak_losses={SYMBOL: 3})
    mock_mt5.deals[1] = [_deal(profit=25.0, price=2402.50)]
    t.check_and_move_breakeven()

    assert t.streak_losses[SYMBOL] == 0
    assert t.trade_throttle.closed_pnls == [pytest.approx(25.0)]
    assert t.active_trades == {}
    assert "💵 PROFIT" in t.bot.messages[0]
    assert sinks.close.calls[0][0][2] == 1_700_000_500  # close time from the deal
    assert sinks.close.calls[0][0][3] == pytest.approx(2402.50)
    assert sinks.purge.count == 1
    assert sinks.events.kwargs[0]["event_type"] == "position_closed"


def test_close_detector_reports_a_loss_and_advances_the_streak(mock_mt5, sinks):
    t = _be_trader({1: _legacy_trade(1)}, streak_losses={SYMBOL: 1})
    # PnL is money, broker-adjusted: profit + swap + commission.
    mock_mt5.deals[1] = [_deal(profit=-15.0, price=2398.00, commission=-1.0)]
    t.check_and_move_breakeven()

    assert t.streak_losses[SYMBOL] == 2
    assert t.trade_throttle.closed_pnls == [pytest.approx(-16.0)]
    assert "🛑 LOSS/BREAKEVEN" in t.bot.messages[0]
    assert "Loss streak: 2" in t.bot.messages[0]


def test_close_detector_falls_back_to_the_cached_pnl_only_for_reporting(mock_mt5, sinks):
    """QUIRK (pinned, not endorsed): with no broker history the loss streak and
    the throttle are driven by ``total_pnl`` (0.0), while the Telegram message
    and the executed_trades row report ``last_close_pnl``. So a cached loss is
    shown to the operator but does NOT advance the streak.

    execution/mt5_trader.py is owner-WIP in this phase and must not be
    modified, so this is documented rather than fixed.
    """
    t = _be_trader({1: _legacy_trade(1)}, last_close_pnl={1: -7.5})
    t.check_and_move_breakeven()
    assert t.trade_throttle.closed_pnls == [pytest.approx(0.0)]
    assert t.streak_losses[SYMBOL] == 0
    assert "$-7.50" in t.bot.messages[0]
    assert sinks.close.calls[0][0][4] == pytest.approx(-7.5)


def test_close_detector_survives_a_failing_history_lookup(mock_mt5, sinks, caplog):
    t = _be_trader({1: _legacy_trade(1)})

    def boom(**kwargs):
        raise RuntimeError("history unavailable")

    mock_mt5.history_deals_get = boom
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.check_and_move_breakeven()

    assert "history_deals_get failed" in caplog.text
    assert t.active_trades == {}  # the ticket is still retired
    assert len(t.bot.messages) == 1  # and still notified


def test_close_detector_handles_several_closed_tickets(mock_mt5, sinks):
    t = _be_trader({1: _legacy_trade(1), 2: _legacy_trade(2)})
    t.check_and_move_breakeven()
    assert len(t.bot.messages) == 2
    assert sinks.close.count == 2
    assert t.active_trades == {}


def test_close_detector_keeps_going_when_the_close_log_fails(mock_mt5, sinks, caplog, monkeypatch):
    """A failing DB write must not swallow the Telegram notification."""
    monkeypatch.setattr(trader_mod, "log_trade_close", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    t = _be_trader({1: _legacy_trade(1)})
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.check_and_move_breakeven()
    assert "Trade close logging failed" in caplog.text
    assert len(t.bot.messages) == 1


def test_close_detector_keeps_going_when_the_context_purge_fails(mock_mt5, sinks, caplog, monkeypatch):
    monkeypatch.setattr(
        trader_mod, "purge_closed_position_context", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )
    t = _be_trader({1: _legacy_trade(1)})
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.check_and_move_breakeven()
    assert "Position context purge failed" in caplog.text
    assert t.active_trades == {}


def test_close_detector_labels_the_leg_in_the_notification(mock_mt5, sinks):
    t = _be_trader({4: _leg_trade(4, 3)})
    t.check_and_move_breakeven()
    assert "(Leg 3)" in t.bot.messages[0]


# ---------------------------------------------------------------------------
# execute_signal — the remaining injected-exception handlers
# ---------------------------------------------------------------------------


def test_execute_signal_treats_a_positions_api_error_as_no_positions(mock_mt5, sinks, monkeypatch):
    """positions_get_by_magic() returning None is an MT5 API error, not "no
    open positions" — the risk budget is consulted with an empty set."""
    seen = []
    monkeypatch.setattr(trader_mod, "positions_get_by_magic", lambda **k: seen.append(k) or None)
    t = _sig_trader()
    t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 3  # trading proceeds


def test_execute_signal_falls_back_to_zero_equity_when_account_info_raises(mock_mt5, sinks):
    """A failed equity lookup only costs the throttle its input; trading
    proceeds with equity 0.0.

    NOTE: account_info() must fail only for this call. Keeping it broken would
    also break _account_fingerprint(), whose "unknown" mode is rejected by
    ExecutionEvent — the deferred defect pinned in step 2c.
    """
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("account_info unavailable")
        return mock_mt5.account

    mock_mt5.account_info = flaky
    t = _sig_trader()
    t.execute_signal(ASSET, _signal())
    assert t.trade_throttle.equities_seen == [0.0]
    assert mock_mt5.call_count("order_send") == 3


def test_execute_signal_dry_run_skips_legs_below_the_lot_step(mock_mt5, sinks, caplog):
    """A base volume under one lot step leaves legs 1-2 unfillable; only leg 3
    is logged in dry-run mode."""
    t = _sig_trader(dry_run=True, volume=0.02)
    with caplog.at_level("INFO", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert "Leg 3 order NOT sent" in caplog.text
    assert "Leg 1 order NOT sent" not in caplog.text


def test_execute_signal_survives_a_failing_intent_persist(mock_mt5, sinks, caplog, monkeypatch):
    """The Wave-0 intent is best-effort: a failure is logged and the order is
    still sent."""
    monkeypatch.setattr(
        trader_mod, "append_signal_intent", lambda *a, **k: (_ for _ in ()).throw(OSError("intent db locked"))
    )
    t = _sig_trader()
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert "Intent persist/enqueue failed" in caplog.text
    assert mock_mt5.call_count("order_send") == 3


def test_execute_signal_falls_back_to_now_for_an_unparseable_timestamp(mock_mt5, sinks):
    t = _sig_trader()
    t.execute_signal(ASSET, _signal(timestamp_utc="not-a-timestamp"))
    assert mock_mt5.call_count("order_send") == 3
    # The entry was still logged, with a sane integer time.
    assert sinks.entry.count == 3
    # log_trade_entry(db_path, ticket, asset_key, bias, entry_time, price, features)
    assert all(isinstance(call[0][4], int) for call in sinks.entry.calls)


def test_execute_signal_survives_a_failing_trade_entry_log(mock_mt5, sinks, caplog, monkeypatch):
    monkeypatch.setattr(trader_mod, "log_trade_entry", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    t = _sig_trader()
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert "Trade entry logging failed" in caplog.text
    assert mock_mt5.call_count("order_send") == 3
    assert t.active_trades  # the leg is still tracked


def test_execute_signal_survives_a_failing_position_context_write(mock_mt5, sinks, caplog, monkeypatch):
    monkeypatch.setattr(
        trader_mod, "record_position_context", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs"))
    )
    t = _sig_trader()
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert "Position context logging failed" in caplog.text
    assert mock_mt5.call_count("order_send") == 3
