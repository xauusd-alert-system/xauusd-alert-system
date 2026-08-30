"""Unit tests for the order-send paths of MultiAssetMT5Trader (Phase 5, step 2d).

Scope (from the term-missing report of step 2c):

    _record_execution_result   execution telemetry, never blocks trading
    _modify_sl_tp              TRADE_ACTION_SLTP modify + its accept/reject events
    _close_partial_position    partial close + market-closed demotion
    execute_signal             reject gates (blackout, routing, allowlist,
                               expiry, signal_state, confidence, correlation,
                               risk manager, throttle, grid step) and the
                               happy path up to and including order_send

Deliberately out of scope (step 2e and later): check_and_move_breakeven(),
run_loop(), __init__(), main().

The trader is built with ``object.__new__`` and only the attributes the code
path under test reads are assigned (same pattern as step 2c) — ``__init__``
loads config, starts the book feed and builds ML pipelines, none of which these
paths touch.

MT5 access goes through mt5_adapter.testing.MockMT5Module. NOTE: mt5_trader
resolves ``mt5 = get_mt5_module()`` at IMPORT time, so patching
``mt5_adapter.lazy.get_mt5_module`` leaves the module handle untouched — the
``mt5`` attribute on the trader module itself must be swapped (same approach as
test_fx_execution_probe.py and test_mt5_trader_helpers.py).

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
BID = 2400.00
ASK = 2400.30

# Retcode MT5 reports for orders sent while the market is closed. MockMT5Module
# does not define it, so _close_partial_position falls back to the 10018
# default; the market-closed test sets it explicitly to stay readable.
RETCODE_MARKET_CLOSED = 10018


# ---------------------------------------------------------------------------
# Recording stubs
# ---------------------------------------------------------------------------


class _Bot:
    """Telegram stand-in: records instead of sending."""

    def __init__(self):
        self.messages = []
        self.alerts = []

    def send_text_message(self, text):
        self.messages.append(text)
        return True

    def send_alert_if_qualified(self, signal, asset_key):
        self.alerts.append((asset_key, signal))
        return True


class _RiskManager:
    def __init__(self, allowed=True, reason=""):
        self.allowed = allowed
        self.reason = reason
        self.recorded = []
        self.seen_counts = []

    def can_trade(self, asset_key, groups_by_asset, singles_by_asset):
        self.seen_counts.append((groups_by_asset, singles_by_asset))
        return self.allowed, self.reason

    def record_trade_executed(self, asset_key):
        self.recorded.append(asset_key)


class _Throttle:
    def __init__(self, ok=True, reason="", risk_multiplier=1.0):
        self.ok = ok
        self.reason = reason
        self._mult = risk_multiplier
        self.equities = []

    def can_trade(self, equity):
        self.equities.append(equity)
        return self.ok, self.reason

    def risk_multiplier(self):
        return self._mult


class _Recorder:
    """Collects (args, kwargs) for every call of a patched module function."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
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
    """Swap the trader module's import-time mt5 handle for a MockMT5Module."""
    mock = MockMT5Module()  # trade_mode defaults to ACCOUNT_TRADE_MODE_DEMO
    mock.initialize()  # account_info()/positions_get() return None until then
    mock.set_symbol_info(SYMBOL, digits=2, point=0.01, trade_stops_level=10, trade_freeze_level=5)
    mock.set_tick(SYMBOL, bid=BID, ask=ASK)

    def positions_get_by_magic(symbol=None, magic=None):
        found = [p for p in mock.positions if magic is None or p.magic == magic]
        if symbol is not None:
            found = [p for p in found if p.symbol == symbol]
        return found

    # Keep the module-level helper consistent with the mock's own positions.
    monkeypatch.setattr(trader_mod, "positions_get_by_magic", positions_get_by_magic)
    monkeypatch.setattr(trader_mod, "mt5", mock)

    # Deterministic fills: the bare mock returns its internal next-ticket
    # counter, which makes per-leg assertions unreadable. Tests that need a
    # different behaviour install their own handler.
    mock.order_send_handler = lambda request: _result()
    return mock


@pytest.fixture()
def env(monkeypatch):
    """Patch every disk/network side channel with a recorder."""
    rec = types.SimpleNamespace(
        intent=_Recorder(),
        ledger=_Recorder(),  # log_execution_attempt
        entry=_Recorder(),  # log_trade_entry
        context=_Recorder(),  # record_position_context
        events=_Recorder(),  # append_trading_event
        outbox=_Recorder(),  # enqueue_event
        validate=_Recorder(),
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
    monkeypatch.setattr(
        trader_mod, "order_routing_allowed", lambda cfg, confirmed_by=None: (routing["allowed"], routing["reason"])
    )
    monkeypatch.setattr(trader_mod, "get_signal_grid", lambda cfg, asset_cfg, regime="": grid["grid"])

    rec.routing = routing
    rec.grid = grid
    return rec


def _trader(cfg=None, *, bot=None, risk=None, throttle=None, volume=0.09, dry_run=False, **overrides):
    t = object.__new__(MultiAssetMT5Trader)
    t.cfg = cfg if cfg is not None else _cfg()
    t.magic_number = MAGIC
    t.volume = volume
    t.dry_run = dry_run
    t.bot = bot if bot is not None else _Bot()
    t.risk_manager = risk if risk is not None else _RiskManager()
    t.trade_throttle = throttle if throttle is not None else _Throttle()
    t.active_trades = {}
    t.signal_features = {}
    t.streak_losses = {}
    t.execution_assets = {ASSET}
    t.order_routing_enabled = True
    t.strategy_identity = {"strategy_version": "sv-1", "config_hash": "cfg-hash-1"}
    t.deployment_mode = types.SimpleNamespace(value="demo_systematic")
    t.trade_db_path = "test-only.db"
    # Correlation filter off by default: it is step 2c's concern and would
    # otherwise fetch candles. It defaults to ENABLED when absent, so it must
    # be switched off explicitly.
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


def _position(ticket=1001, symbol=SYMBOL, type=0, volume=0.09, magic=MAGIC, sl=0.0, tp=0.0):
    return types.SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        type=type,
        volume=volume,
        sl=sl,
        tp=tp,
        magic=magic,
    )


# ---------------------------------------------------------------------------
# _record_execution_result
# ---------------------------------------------------------------------------


def test_record_result_logs_a_fill(mock_mt5, env):
    t = _trader()
    t._record_execution_result(
        asset_key=ASSET,
        broker_symbol=SYMBOL,
        action="open",
        side="buy",
        requested_at=1_700_000_000_000,
        request={"price": ASK, "volume": 0.03, "comment": "L1"},
        result=_result(),
        position_ticket=1001,
        intent_id="intent-1",
    )
    assert env.ledger.count == 1
    _, kw = env.ledger.calls[0]
    assert kw["asset_key"] == ASSET
    assert kw["broker_symbol"] == SYMBOL
    assert kw["status"] == "filled"
    assert kw["retcode"] == TRADE_RETCODE_DONE
    assert kw["filled_price"] == pytest.approx(ASK)
    assert kw["volume_filled"] == pytest.approx(0.03)
    assert kw["requested_price"] == pytest.approx(ASK)
    assert kw["position_ticket"] == 1001
    assert kw["intent_id"] == "intent-1"
    assert kw["rejection_reason"] is None
    assert kw["metadata"] == {"comment": "L1"}


def test_record_result_logs_a_rejection(mock_mt5, env):
    t = _trader()
    t._record_execution_result(
        asset_key=ASSET,
        broker_symbol=SYMBOL,
        action="partial_close",
        side="sell",
        requested_at=1_700_000_000_000,
        request={"price": BID, "volume": 0.03},
        result=_result(retcode=TRADE_RETCODE_REJECT, order=0, deal=0, volume=0.0, price=0.0, comment="no money"),
    )
    _, kw = env.ledger.calls[0]
    assert kw["status"] == "rejected"
    assert kw["filled_price"] is None  # a rejection has no fill
    assert kw["volume_filled"] is None
    assert kw["rejection_reason"] == "no money"


def test_record_result_never_invents_slippage_from_zero_prices(mock_mt5, env):
    """Some MT5 result types report 0 for price/volume even on a DONE retcode;
    that must be recorded as "no fill data", not as a 0.0 fill."""
    t = _trader()
    t._record_execution_result(
        asset_key=ASSET,
        broker_symbol=SYMBOL,
        action="open",
        side="buy",
        requested_at=1_700_000_000_000,
        request={"price": ASK, "volume": 0.03},
        result=_result(price=0.0, volume=0.0),
    )
    _, kw = env.ledger.calls[0]
    assert kw["status"] == "filled"
    assert kw["filled_price"] is None
    assert kw["volume_filled"] is None


def test_record_result_marks_a_partial_fill(mock_mt5, env, monkeypatch):
    """TRADE_RETCODE_DONE_PARTIAL is not defined on MockMT5Module, so the
    'partial' status is unreachable with the bare mock — define it to pin the
    branch (real terminals do report it)."""
    monkeypatch.setattr(trader_mod.mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010, raising=False)
    t = _trader()
    t._record_execution_result(
        asset_key=ASSET,
        broker_symbol=SYMBOL,
        action="open",
        side="buy",
        requested_at=1_700_000_000_000,
        request={"price": ASK, "volume": 0.03},
        result=_result(retcode=10010),
    )
    assert env.ledger.kwargs[0]["status"] == "partial"


def test_record_result_swallows_ledger_failures(mock_mt5, env, monkeypatch, caplog):
    """Telemetry is best-effort: a failing ledger must never reach the caller."""
    monkeypatch.setattr(
        trader_mod, "log_execution_attempt", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    t = _trader()
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t._record_execution_result(
            asset_key=ASSET,
            broker_symbol=SYMBOL,
            action="open",
            side="buy",
            requested_at=1_700_000_000_000,
            request={"price": ASK, "volume": 0.03},
            result=_result(),
        )
    assert "Execution ledger write failed" in caplog.text


# ---------------------------------------------------------------------------
# _modify_sl_tp
# ---------------------------------------------------------------------------


def test_modify_sl_tp_accepts_and_records_both_events(mock_mt5, env):
    t = _trader()
    pos = _position(ticket=4242, sl=2399.0, tp=2402.0)
    assert t._modify_sl_tp(pos, 2400.30, 2405.0) is True

    sent = [c for c in mock_mt5.calls if c[0] == "order_send"]
    assert len(sent) == 1
    request = sent[0][1][0]
    assert request["action"] == TRADE_ACTION_SLTP
    assert request["position"] == 4242
    assert request["symbol"] == SYMBOL
    assert request["sl"] == pytest.approx(2400.30)
    assert request["tp"] == pytest.approx(2405.0)

    kinds = [k["event_type"] for _, k in env.events.calls]
    assert kinds == ["stop_move_requested", "stop_move_confirmed"]
    requested = env.events.kwargs[0]
    assert requested["position_ticket"] == 4242
    assert requested["payload"]["old_sl"] == pytest.approx(2399.0)
    assert requested["payload"]["new_sl"] == pytest.approx(2400.30)
    assert env.events.kwargs[1]["reason"] == "broker_confirmed"


def test_modify_sl_tp_reports_a_broker_rejection(mock_mt5, env):
    t = _trader()
    mock_mt5.order_send_handler = lambda req: _result(
        retcode=TRADE_RETCODE_REJECT, order=0, deal=0, volume=0.0, price=0.0, comment="invalid stops"
    )
    pos = _position(ticket=4242)
    assert t._modify_sl_tp(pos, 2400.30, 2405.0) is False

    kinds = [k["event_type"] for _, k in env.events.calls]
    assert kinds == ["stop_move_requested", "stop_move_rejected"]
    assert env.events.kwargs[1]["reason"] == "invalid stops"
    assert env.events.kwargs[1]["payload"]["retcode"] == TRADE_RETCODE_REJECT


def test_modify_sl_tp_in_dry_run_skips_order_send(mock_mt5, env):
    t = _trader(dry_run=True)
    assert t._modify_sl_tp(_position(), 2400.30, 2405.0) is True
    assert mock_mt5.call_count("order_send") == 0
    assert env.events.count == 0


def test_modify_sl_tp_uses_the_tracked_trade_symbol_for_the_event(mock_mt5, env):
    """The event is attributed to the asset the trade was opened for, which is
    not necessarily the broker symbol."""
    t = _trader()
    t.active_trades[4242] = {"symbol": ASSET, "signal_contract": {"signal_id": "sig-9"}}
    t._modify_sl_tp(_position(ticket=4242, symbol="XAUUSD.m"), 2400.30, 2405.0)
    assert env.events.kwargs[0]["asset_key"] == ASSET
    assert env.events.kwargs[0]["signal_id"] == "sig-9"


# ---------------------------------------------------------------------------
# _close_partial_position
# ---------------------------------------------------------------------------


def test_close_partial_sells_a_long_position(mock_mt5, env):
    t = _trader()
    pos = _position(ticket=4242, type=0, volume=0.09)
    assert t._close_partial_position(pos, BID, 0.03, "TP1") is True

    request = mock_mt5.calls[-1][1][0]
    assert request["action"] == TRADE_ACTION_DEAL
    assert request["type"] == ORDER_TYPE_SELL  # closing a long is a sell
    assert request["position"] == 4242
    assert request["symbol"] == SYMBOL
    assert request["volume"] == pytest.approx(0.03)
    assert request["price"] == pytest.approx(BID)
    assert request["magic"] == MAGIC
    assert request["comment"] == "TP1 close"

    kinds = [k["event_type"] for _, k in env.events.calls]
    assert kinds == ["partial_close_submitted", "partial_filled"]
    assert env.events.kwargs[1]["order_ticket"] == 555
    # The close is also written to the execution ledger.
    assert env.ledger.kwargs[0]["action"] == "partial_close"
    assert env.ledger.kwargs[0]["side"] == "sell"
    assert env.ledger.kwargs[0]["position_ticket"] == 4242


def test_close_partial_buys_back_a_short_position(mock_mt5, env):
    t = _trader()
    assert t._close_partial_position(_position(type=1), ASK, 0.03, "TP2") is True
    assert mock_mt5.calls[-1][1][0]["type"] == ORDER_TYPE_BUY
    assert env.ledger.kwargs[0]["side"] == "buy"


def test_close_partial_reports_a_rejection(mock_mt5, env):
    t = _trader()
    mock_mt5.order_send_handler = lambda req: _result(
        retcode=TRADE_RETCODE_REJECT, order=0, deal=0, volume=0.0, price=0.0, comment="requote"
    )
    assert t._close_partial_position(_position(), BID, 0.03, "TP1") is False

    kinds = [k["event_type"] for _, k in env.events.calls]
    assert kinds == ["partial_close_submitted", "partial_rejected"]
    assert env.events.kwargs[1]["reason"] == "requote"
    assert env.events.kwargs[1]["payload"] == {"retcode": TRADE_RETCODE_REJECT, "label": "TP1"}


def test_close_partial_demotes_market_closed_when_quiet(mock_mt5, env, caplog):
    """Blackout flatten passes run on weekends/holidays, when every order comes
    back 10018. That expected case must not raise an event storm."""
    mock_mt5.TRADE_RETCODE_MARKET_CLOSED = RETCODE_MARKET_CLOSED
    t = _trader()
    mock_mt5.order_send_handler = lambda req: _result(
        retcode=RETCODE_MARKET_CLOSED, order=0, deal=0, volume=0.0, price=0.0, comment="market closed"
    )
    with caplog.at_level("INFO", logger=TRADER_LOG):
        got = t._close_partial_position(_position(), BID, 0.03, "blackout-halt", quiet_market_closed=True)
    assert got is False
    kinds = [k["event_type"] for _, k in env.events.calls]
    assert kinds == ["partial_close_submitted"]  # no partial_rejected
    assert "deferred to next session" in caplog.text


def test_close_partial_still_flags_market_closed_when_not_quiet(mock_mt5, env):
    """The same retcode outside a blackout pass is unexpected and must be
    reported as a rejection."""
    mock_mt5.TRADE_RETCODE_MARKET_CLOSED = RETCODE_MARKET_CLOSED
    t = _trader()
    mock_mt5.order_send_handler = lambda req: _result(
        retcode=RETCODE_MARKET_CLOSED, order=0, deal=0, volume=0.0, price=0.0, comment="market closed"
    )
    assert t._close_partial_position(_position(), BID, 0.03, "TP1") is False
    kinds = [k["event_type"] for _, k in env.events.calls]
    assert "partial_rejected" in kinds


def test_close_partial_in_dry_run_skips_order_send(mock_mt5, env):
    t = _trader(dry_run=True)
    assert t._close_partial_position(_position(), BID, 0.03, "TP1") is True
    assert mock_mt5.call_count("order_send") == 0
    assert env.events.count == 0
    assert env.ledger.count == 0


# ---------------------------------------------------------------------------
# execute_signal — reject gates
# ---------------------------------------------------------------------------


def test_execute_signal_ignores_a_no_trade_bias(mock_mt5, env):
    t = _trader()
    t.execute_signal(ASSET, _signal(bias="no_trade"))
    assert mock_mt5.call_count("order_send") == 0


def test_execute_signal_stops_inside_the_weekend_blackout(mock_mt5, env, caplog):
    cfg = _cfg(enabled=True, weekend={"start_dow": 4, "start_utc": "21:00", "end_dow": 0, "end_utc": "21:00"})
    t = _trader(cfg)
    t.blackout_manual_until = None
    # Force "now" into the window by pinning the weekend start in the past.
    t._blackout_status = lambda now: (True, "weekend blackout", None)
    t._in_daily_break = lambda now: False
    with caplog.at_level("INFO", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "Signal skipped" in caplog.text


def test_execute_signal_stops_inside_the_daily_break(mock_mt5, env, caplog):
    t = _trader(_cfg(enabled=True))
    t._blackout_status = lambda now: (False, None, None)
    t._in_daily_break = lambda now: True
    with caplog.at_level("INFO", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "in daily break" in caplog.text


def test_execute_signal_respects_disabled_order_routing(mock_mt5, env, caplog):
    t = _trader()
    t.order_routing_enabled = False
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "Order routing blocked" in caplog.text


def test_execute_signal_respects_a_refused_deployment_mode(mock_mt5, env, caplog):
    env.routing["allowed"] = False
    env.routing["reason"] = "deployment_mode=research"
    t = _trader()
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "deployment_mode=research" in caplog.text


def test_execute_signal_respects_the_execution_allowlist(mock_mt5, env, caplog):
    t = _trader()
    t.execution_assets = {"EURUSD"}  # XAUUSD not allowed
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "execution allowlist" in caplog.text


def test_execute_signal_drops_an_expired_signal(mock_mt5, env, caplog):
    t = _trader()
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal(expires_at_utc=1))
    assert mock_mt5.call_count("order_send") == 0
    assert "expired before execution" in caplog.text


def test_execute_signal_requires_a_confirmed_signal_state(mock_mt5, env, caplog):
    t = _trader()
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal(signal_state="pending"))
    assert mock_mt5.call_count("order_send") == 0
    assert "is not confirmed" in caplog.text


def test_execute_signal_accepts_explicit_none_signal_state(mock_mt5, env):
    """signal_state is optional: None is treated as confirmed (legacy signals)."""
    t = _trader()
    t.execute_signal(ASSET, _signal(signal_state=None))
    assert mock_mt5.call_count("order_send") == 3


def test_execute_signal_suppresses_low_confidence(mock_mt5, env, caplog):
    t = _trader()
    with caplog.at_level("INFO", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal(confidence=0.50))
    assert mock_mt5.call_count("order_send") == 0
    assert "dynamic threshold" in caplog.text


def test_execute_signal_uses_the_dynamic_threshold(mock_mt5, env):
    """The threshold escalates with the loss streak, so a confidence that
    passes cleanly can be suppressed after two consecutive losses."""
    t = _trader()
    t.streak_losses = {ASSET: 3}  # +0.06 -> 0.66
    t.execute_signal(ASSET, _signal(confidence=0.63))
    assert mock_mt5.call_count("order_send") == 0
    t.execute_signal(ASSET, _signal(confidence=0.70))
    assert mock_mt5.call_count("order_send") == 3


def test_execute_signal_respects_the_correlation_filter(mock_mt5, env, caplog):
    t = _trader()
    t._has_correlated_position = lambda asset_key, bias: True
    with caplog.at_level("INFO", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "correlation filter" in caplog.text.lower()


def test_execute_signal_respects_the_risk_manager(mock_mt5, env, caplog):
    t = _trader(risk=_RiskManager(allowed=False, reason="Max concurrent positions limit reached (6/6)"))
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "Max concurrent positions limit reached" in caplog.text


def test_execute_signal_counts_a_leg_group_as_one_risk_slot(mock_mt5, env):
    """The risk budget is per group, not per leg: three legs sharing one
    group_key must consume a single slot."""
    t = _trader()
    # Positions are on a different symbol: execute_signal refuses to open a
    # second group on the SAME symbol, which would short-circuit before the
    # risk budget is consulted.
    for ticket in (1, 2, 3):
        mock_mt5.add_position("SILVER", ticket=ticket)
        t.active_trades[ticket] = {"symbol": ASSET, "group_key": f"{ASSET}:sig-0"}
    t.execute_signal(ASSET, _signal())
    groups, singles = t.risk_manager.seen_counts[0]
    assert groups == {ASSET: {f"{ASSET}:sig-0"}}
    assert singles == {}


def test_execute_signal_respects_the_trade_throttle(mock_mt5, env, caplog):
    t = _trader(throttle=_Throttle(ok=False, reason="daily loss limit hit"))
    with caplog.at_level("WARNING", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "daily loss limit hit" in caplog.text


def test_execute_signal_reads_equity_for_the_throttle(mock_mt5, env):
    t = _trader()
    t.execute_signal(ASSET, _signal())
    # MockMT5Module reports equity 10000 once initialized.
    assert t.trade_throttle.equities == [pytest.approx(10000.0)]


def test_execute_signal_falls_back_to_zero_equity_without_account_info(mock_mt5, env):
    t = _trader()
    mock_mt5._initialized = False  # account_info() returns None
    t.execute_signal(ASSET, _signal())
    assert t.trade_throttle.equities == [0.0]


def test_execute_signal_refuses_a_signal_without_a_positive_grid_step(mock_mt5, env, caplog):
    t = _trader()
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal(step=0.0))
    assert mock_mt5.call_count("order_send") == 0
    assert "no positive grid step" in caplog.text


def test_execute_signal_bails_out_when_initialize_fails(mock_mt5, env):
    t = _trader()
    mock_mt5.initialize = lambda *a, **k: False
    t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0


def test_execute_signal_skips_a_symbol_that_already_has_a_position(mock_mt5, env):
    t = _trader()
    mock_mt5.add_position(SYMBOL, ticket=1)
    t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert env.validate.count == 1  # the symbol was still validated first


def test_execute_signal_bails_out_without_market_data(mock_mt5, env):
    t = _trader()
    mock_mt5.ticks.pop(SYMBOL)
    t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0


def test_execute_signal_bails_out_when_stop_normalization_fails(mock_mt5, env, caplog):
    t = _trader()
    t._normalize_stops = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("symbol_info failed for GOLD"))
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert "Normalization failed" in caplog.text


# ---------------------------------------------------------------------------
# execute_signal — happy path up to order_send
# ---------------------------------------------------------------------------


def test_execute_signal_sends_three_equal_legs(mock_mt5, env):
    t = _trader()
    t.execute_signal(ASSET, _signal())

    sent = [c[1][0] for c in mock_mt5.calls if c[0] == "order_send"]
    assert len(sent) == 3
    assert [r["volume"] for r in sent] == pytest.approx([0.03, 0.03, 0.03])
    assert all(r["action"] == TRADE_ACTION_DEAL for r in sent)
    assert all(r["symbol"] == SYMBOL for r in sent)
    assert all(r["magic"] == MAGIC for r in sent)
    assert all(r["type"] == 0 for r in sent)  # ORDER_TYPE_BUY for a long
    assert all(r["price"] == pytest.approx(ASK) for r in sent)


def test_execute_signal_attaches_the_shared_sl_and_a_per_leg_tp(mock_mt5, env):
    """price 2400.30, step 1.0 -> SL 2398.30 (2 steps), TPs at +1/+2/+3."""
    t = _trader()
    t.execute_signal(ASSET, _signal())
    sent = [c[1][0] for c in mock_mt5.calls if c[0] == "order_send"]
    assert all(r["sl"] == pytest.approx(2398.30) for r in sent)
    assert [r["tp"] for r in sent] == pytest.approx([2401.30, 2402.30, 2403.30])


def test_execute_signal_carries_the_intent_id_in_every_leg_comment(mock_mt5, env):
    """The MQL5 observer joins broker deal facts back to the intent through the
    8-char short id embedded in the order comment."""
    t = _trader()
    t.execute_signal(ASSET, _signal())
    sent = [c[1][0] for c in mock_mt5.calls if c[0] == "order_send"]
    intent_id = env.intent.calls[0][0][1].intent_id
    assert [r["comment"] for r in sent] == [
        f"{ASSET} ML Scalp L1 {intent_id[:8]}",
        f"{ASSET} ML Scalp L2 {intent_id[:8]}",
        f"{ASSET} ML Scalp L3 {intent_id[:8]}",
    ]


def test_execute_signal_persists_the_intent_before_sending(mock_mt5, env):
    """Wave-0 contract: the immutable SignalIntent must exist before any
    broker request can be observed."""
    order = []

    def spy(request):
        order.append(("order_send", len(env.intent.calls)))
        return _result()

    mock_mt5.order_send_handler = spy
    t = _trader()
    t.execute_signal(ASSET, _signal())
    assert env.intent.count == 1
    # The intent was already persisted when the first order_send went out.
    assert order[0][1] == 1


def test_execute_signal_emits_the_wave0_facts(mock_mt5, env):
    t = _trader()
    t.execute_signal(ASSET, _signal())
    # 1 intent_created + 3 request_result facts (one per leg).
    assert env.outbox.count == 4
    intent_id = env.intent.calls[0][0][1].intent_id
    intent_fact = env.outbox.calls[0][0][1]
    assert intent_fact.event_type == "intent_created"
    assert intent_fact.intent_id == intent_id
    facts = [call[0][1] for call in env.outbox.calls[1:]]
    assert all(f.event_type == "request_result" for f in facts)
    assert all(f.intent_id == intent_id for f in facts)


def test_execute_signal_tracks_every_filled_leg(mock_mt5, env):
    t = _trader()
    t.execute_signal(ASSET, _signal())
    assert set(t.active_trades) == {555}
    trade = t.active_trades[555]
    assert trade["symbol"] == ASSET
    assert trade["type"] == "long"
    assert trade["leg"] == 3  # last leg wins the shared ticket in the mock
    assert trade["tp1"] == pytest.approx(2403.30)
    assert trade["group_key"] == f"{ASSET}:sig-1"
    assert set(t.signal_features) == {555}


def test_execute_signal_resolves_the_position_ticket_from_the_broker(mock_mt5, env):
    """The order ticket differs from the position ticket in real MT5; the
    position ticket is what check_and_move_breakeven() keys on."""

    def handler(request):
        mock_mt5.add_position(SYMBOL, ticket=90210)
        return _result()

    mock_mt5.order_send_handler = handler
    t = _trader()
    t.execute_signal(ASSET, _signal())
    assert set(t.active_trades) == {90210}


def test_execute_signal_notifies_and_records_a_fill(mock_mt5, env):
    t = _trader()
    t.execute_signal(ASSET, _signal())
    assert len(t.bot.messages) == 3
    assert "LEG 1/3 EXECUTED" in t.bot.messages[0]
    assert len(t.bot.alerts) == 1
    assert t.risk_manager.recorded == [ASSET]
    assert t.saves == [1]  # management state persisted immediately
    assert env.entry.count == 3  # log_trade_entry per leg
    assert env.context.count == 3  # position context per leg


def test_execute_signal_logs_each_leg_to_the_execution_ledger(mock_mt5, env):
    t = _trader()
    t.execute_signal(ASSET, _signal())
    assert env.ledger.count == 3
    assert all(kw["action"] == "open" for kw in env.ledger.kwargs)
    assert all(kw["side"] == "buy" for kw in env.ledger.kwargs)


def test_execute_signal_recenters_legs_on_a_drifting_price(mock_mt5, env):
    """Between leg fills the market can move past the signal-time levels and
    the broker then rejects SL/TP with retcode 10016. Each leg's SL/TP is
    shifted by the drift, preserving the entry->SL / entry->TP distances."""
    t = _trader()
    mock_mt5.order_send_handler = lambda req: mock_mt5.set_tick(SYMBOL, bid=BID + 1.0, ask=ASK + 1.0) or _result()
    t.execute_signal(ASSET, _signal())
    sent = [c[1][0] for c in mock_mt5.calls if c[0] == "order_send"]
    # Leg 1 uses the signal-time levels; the tick moves +1.00 during its fill,
    # so legs 2 and 3 are recentred by that drift.
    assert [r["sl"] for r in sent] == pytest.approx([2398.30, 2399.30, 2399.30])
    assert [r["tp"] for r in sent] == pytest.approx([2401.30, 2403.30, 2404.30])
    # The entry->SL and entry->TP distances of the signal geometry survive:
    # leg 2 keeps its own +2.00 step on top of the drift.
    assert sent[1]["tp"] - sent[1]["sl"] == pytest.approx(4.00)


def test_execute_signal_keeps_signal_time_levels_when_the_live_tick_raises(mock_mt5, env, caplog):
    """A live-tick lookup that raises (terminal hiccup) must fall back to the
    signal-time levels instead of aborting the remaining legs."""
    t = _trader()
    real_tick = mock_mt5.symbol_info_tick

    def broken_tick(symbol):
        raise RuntimeError("terminal disconnected")

    def handler(request):
        result = _result()
        mock_mt5.symbol_info_tick = broken_tick
        return result

    mock_mt5.order_send_handler = handler
    try:
        with caplog.at_level("WARNING", logger=TRADER_LOG):
            t.execute_signal(ASSET, _signal())
    finally:
        mock_mt5.symbol_info_tick = real_tick

    sent = [c[1][0] for c in mock_mt5.calls if c[0] == "order_send"]
    assert len(sent) == 3  # all legs still went out
    assert [r["sl"] for r in sent] == pytest.approx([2398.30, 2398.30, 2398.30])
    assert [r["tp"] for r in sent] == pytest.approx([2401.30, 2402.30, 2403.30])
    assert "Live tick unavailable" in caplog.text


def test_execute_signal_steps_the_volume_down_under_throttle_pressure(mock_mt5, env, caplog):
    t = _trader(throttle=_Throttle(risk_multiplier=0.5), volume=0.30)
    with caplog.at_level("INFO", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())
    sent = [c[1][0] for c in mock_mt5.calls if c[0] == "order_send"]
    # 0.30 * 0.5 = 0.15, split the same way as any other volume: leg 3 absorbs
    # the lot-step remainder, so the legs still sum to the stepped-down volume.
    assert [r["volume"] for r in sent] == pytest.approx([0.04, 0.04, 0.07])
    assert sum(r["volume"] for r in sent) == pytest.approx(0.15)
    assert "risk step-down" in caplog.text
    # The configured volume is restored afterwards, so the next signal is
    # unaffected by a transient step-down.
    assert t.volume == pytest.approx(0.30)


def test_execute_signal_restores_the_volume_after_a_dry_run_step_down(mock_mt5, env):
    t = _trader(dry_run=True, throttle=_Throttle(risk_multiplier=0.5), volume=0.30)
    t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert t.volume == pytest.approx(0.30)


def test_execute_signal_in_dry_run_sends_nothing(mock_mt5, env):
    t = _trader(dry_run=True)
    t.execute_signal(ASSET, _signal())
    assert mock_mt5.call_count("order_send") == 0
    assert env.intent.count == 0
    assert t.bot.messages == []
    assert t.risk_manager.recorded == []


def test_execute_signal_reports_a_rejected_leg(mock_mt5, env, caplog):
    """A rejected leg must be recorded as rejected and must not register a
    position or notify Telegram."""
    t = _trader()
    mock_mt5.order_send_handler = lambda req: _result(
        retcode=TRADE_RETCODE_REJECT, order=0, deal=0, volume=0.0, price=0.0, comment="invalid volume"
    )
    with caplog.at_level("ERROR", logger=TRADER_LOG):
        t.execute_signal(ASSET, _signal())

    assert mock_mt5.call_count("order_send") == 3  # every leg is still attempted
    kinds = [k["event_type"] for _, k in env.events.calls]
    assert kinds.count("order_rejected") == 3
    assert "order_filled" not in kinds
    assert t.active_trades == {}
    assert t.bot.messages == []
    assert t.risk_manager.recorded == []
    assert t.saves == []
    assert env.entry.count == 0
    assert "Order Send Failed" in caplog.text


def test_execute_signal_continues_past_a_rejected_leg(mock_mt5, env):
    """Only leg 2 is rejected; legs 1 and 3 still fill and are tracked."""
    t = _trader()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 2:
            return _result(retcode=TRADE_RETCODE_REJECT, order=0, deal=0, volume=0.0, price=0.0, comment="requote")
        return _result(order=500 + calls["n"])

    mock_mt5.order_send_handler = handler
    t.execute_signal(ASSET, _signal())
    kinds = [k["event_type"] for _, k in env.events.calls]
    assert kinds.count("order_filled") == 2
    assert kinds.count("order_rejected") == 1
    assert len(t.bot.messages) == 2
    assert len(t.risk_manager.recorded) == 1


def test_execute_signal_skips_legs_that_round_below_the_lot_step(mock_mt5, env):
    """A base volume below one lot step leaves legs 1-2 unfillable; only leg 3
    is sent and it carries the whole volume."""
    t = _trader(volume=0.02)
    t.execute_signal(ASSET, _signal())
    sent = [c[1][0] for c in mock_mt5.calls if c[0] == "order_send"]
    assert [r["volume"] for r in sent] == pytest.approx([0.02])


def test_execute_signal_short_side_geometry(mock_mt5, env):
    t = _trader()
    t.execute_signal(ASSET, _signal(bias="short"))
    sent = [c[1][0] for c in mock_mt5.calls if c[0] == "order_send"]
    assert len(sent) == 3
    assert all(r["type"] == ORDER_TYPE_SELL for r in sent)
    assert all(r["price"] == pytest.approx(BID) for r in sent)  # shorts fill at bid
    # price 2400.00, step 1.0 -> SL 2402.00, TPs at -1/-2/-3
    assert all(r["sl"] == pytest.approx(2402.00) for r in sent)
    assert [r["tp"] for r in sent] == pytest.approx([2399.00, 2398.00, 2397.00])
