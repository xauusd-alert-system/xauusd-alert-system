"""Regression tests for the 3-LEG breakeven fix (audit 2026-08-18).

Production bug (XAGUSD group, 2026-08-18): after the TP1 leg closed, the SL
of legs 2/3 was never moved to entry and the legs were stopped out at the
original SL (-$274 each). Two root causes, both covered here:

1. _move_sl_to_entry() returned silently when the entry target was closer
   than _get_min_dist() (which pads the distance with spread + 30 pts),
   instead of clamping to the broker's own minimum (trade_stops_level).
2. The 3-LEG BE block set be_done=True unconditionally, so a blocked
   breakeven was never retried.

Expected behaviour after the fix:
- the SL is clamped to the broker minimum (bid + stops_level for sells,
  ask - stops_level for buys) and the move is sent anyway;
- _move_sl_to_entry() returns True only when the broker accepted the move;
- be_done is set only on acceptance, and the per-tick retry keeps pulling
  the SL tighter until a true breakeven becomes reachable.
"""
import types

from execution import mt5_trader as trader_mod
from execution.mt5_trader import MultiAssetMT5Trader

# Broker facts probed on the live FxPro account (2026-08-18):
# XAGUSD: trade_stops_level = 20 pts ($0.02), XAUUSD: 0.
SYMBOLS = {
    "XAGUSD": dict(bid=64.398, ask=64.419, digits=3, point=0.001, stops=20),
    "XAUUSD": dict(bid=4393.39, ask=4393.40, digits=2, point=0.01, stops=0),
}


class _FakeBot:
    def __init__(self):
        self.messages = []

    def send_text_message(self, text):
        self.messages.append(text)
        return True


def _pos(ticket, symbol, type_, entry, sl, tp=None):
    return types.SimpleNamespace(
        ticket=ticket, symbol=symbol, type=type_, price_open=entry,
        volume=0.33, sl=sl, tp=tp, time=1_700_000_000,
    )


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
    t.be_state = {}
    t.be_trigger_by_symbol = {}
    t.trailing_atr_mult_by_symbol = {}
    t.streak_losses = {}
    t.signal_features = {}
    t.last_close_pnl = {}
    t.dry_run = False
    t.bot = _FakeBot()
    # Attributes added to the trader after these harnesses were written;
    # minimal stand-ins keep the unit under test focused on BE logic.
    t.cfg = {"assets": {}}
    t.strategy_identity = "test-identity"
    t.trade_throttle = _StubThrottle()
    t.management_state_path = str(tmp_path / "mgmt_state.json")
    t.trade_db_path = str(tmp_path / "trades.sqlite")
    return t


def _fake_world(monkeypatch, trader, positions, deals_by_ticket=None, symbol_ticks=None):
    """Wire the fake MT5 world; returns the captured order_send requests."""
    sends = []
    ticks = {k: dict(v) for k, v in SYMBOLS.items()}
    for sym, overrides in (symbol_ticks or {}).items():
        ticks[sym].update(overrides)

    def fake_tick(symbol):
        s = ticks[symbol]
        return types.SimpleNamespace(bid=s["bid"], ask=s["ask"])

    def fake_info(symbol):
        s = SYMBOLS[symbol]
        return types.SimpleNamespace(
            point=s["point"], digits=s["digits"],
            trade_stops_level=s["stops"], trade_freeze_level=0,
        )

    def fake_order_send(request):
        sends.append(dict(request))
        return types.SimpleNamespace(
            retcode=trader_mod.mt5.TRADE_RETCODE_DONE, comment="done", order=0,
        )

    monkeypatch.setattr(trader_mod.mt5, "initialize", lambda *a, **k: True)
    monkeypatch.setattr(trader_mod, "positions_get_by_magic", lambda *a, **k: positions)
    monkeypatch.setattr(trader_mod.mt5, "history_deals_get",
                        lambda position=None: (deals_by_ticket or {}).get(position, []))
    monkeypatch.setattr(trader_mod.mt5, "symbol_info_tick", fake_tick)
    monkeypatch.setattr(trader_mod.mt5, "symbol_info", fake_info)
    monkeypatch.setattr(trader_mod.mt5, "order_send", fake_order_send)
    monkeypatch.setattr(trader, "_append_trade_event", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod, "log_trade_close", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod, "purge_closed_position_context", lambda *a, **k: None)
    return sends


def _deal(profit, entry=0, price=64.317, ts=1_700_000_000):
    return types.SimpleNamespace(
        profit=profit, swap=-1.0, commission=-1.0, entry=entry, time=ts, price=price,
    )


LEG2 = {"symbol": "XAGUSD", "type": "short", "entry_price": 64.398,
        "original_volume": 0.33, "leg": 2, "group_key": "G1",
        "tp1": 64.317, "tp2": None, "tp3": None,
        "tp1_hit": False, "tp2_hit": False}
LEG3 = {"symbol": "XAGUSD", "type": "short", "entry_price": 64.398,
        "original_volume": 0.34, "leg": 3, "group_key": "G1",
        "tp1": 64.231, "tp2": None, "tp3": None,
        "tp1_hit": False, "tp2_hit": False}


def test_silver_short_be_clamps_to_broker_minimum_instead_of_abandoning(monkeypatch, tmp_path):
    """The exact XAGUSD scenario: entry == bid, broker minimum 20 pts from
    BID -> a true breakeven (entry - 1 tick) is impossible, but the SL must
    still be pulled to bid + 20 pts (64.418) instead of staying at the
    original 64.551."""
    t = _trader(tmp_path, {2: dict(LEG2)})
    sends = _fake_world(monkeypatch, t, positions=[_pos(2, "XAGUSD", 1, 64.398, 64.551)])

    ok = t._move_sl_to_entry(_pos(2, "XAGUSD", 1, 64.398, 64.551), 64.398)

    assert ok is False  # clamped, not yet a true breakeven -> caller retries
    assert sends and sends[-1]["sl"] == 64.418  # bid 64.398 + 20 pts


def test_gold_long_be_reaches_true_entry_and_returns_true(monkeypatch, tmp_path):
    """XAUUSD has no minimum stop distance: the SL moves to entry + 1 tick
    and the move is reported as accepted."""
    t = _trader(tmp_path, {11: dict(LEG2, symbol="XAUUSD", type="long",
                                    entry_price=4393.03)})
    sends = _fake_world(monkeypatch, t, positions=[_pos(11, "XAUUSD", 0, 4393.03, 4388.00)])

    ok = t._move_sl_to_entry(_pos(11, "XAUUSD", 0, 4393.03, 4388.00), 4393.03)

    assert ok is True
    assert sends and sends[-1]["sl"] == 4393.04  # entry + 1 tick


def test_no_modify_spam_when_sl_already_at_broker_minimum(monkeypatch, tmp_path):
    """The retry runs every BE CHECK (~30s): once the SL already sits at the
    tightest broker-allowed level, no new order may be sent."""
    t = _trader(tmp_path, {2: dict(LEG2)})
    sends = _fake_world(monkeypatch, t, positions=[_pos(2, "XAGUSD", 1, 64.398, 64.418)])

    ok = t._move_sl_to_entry(_pos(2, "XAGUSD", 1, 64.398, 64.418), 64.398)

    assert ok is False
    assert sends == []


def test_be_retry_every_be_check_until_legs_pulled_to_minimum(monkeypatch, tmp_path):
    """Leg 1 is already closed (no active_trade with leg 1 in the group):
    the per-tick retry block must move the SL of legs 2/3 toward entry."""
    t = _trader(tmp_path, {2: dict(LEG2), 3: dict(LEG3)})
    sends = _fake_world(
        monkeypatch, t,
        positions=[_pos(2, "XAGUSD", 1, 64.398, 64.551), _pos(3, "XAGUSD", 1, 64.398, 64.540)],
    )

    t.check_and_move_breakeven()

    sls = sorted(s["sl"] for s in sends)
    assert sls == [64.418, 64.418]  # both legs clamped to bid + 20 pts
    assert not t.active_trades[2].get("be_done")  # clamped -> keep retrying
    assert not t.active_trades[3].get("be_done")


def test_be_retry_reaches_true_breakeven_and_marks_done(monkeypatch, tmp_path):
    """Once price retraces past the minimum distance, the retry moves the SL
    to a true breakeven (entry - 1 tick) and be_done is set."""
    t = _trader(tmp_path, {2: dict(LEG2)})
    sends = _fake_world(
        monkeypatch, t,
        positions=[_pos(2, "XAGUSD", 1, 64.398, 64.418)],
        symbol_ticks={"XAGUSD": {"bid": 64.370}},  # entry - 28 pts: BE reachable
    )
    t.check_and_move_breakeven()

    assert sends and sends[-1]["sl"] == 64.397  # entry - 1 tick
    assert t.active_trades[2]["be_done"] is True


def test_close_detector_be_block_sets_done_only_on_acceptance(monkeypatch, tmp_path):
    """TP1 leg closes at the broker (leg 1 missing from positions): the close
    detector triggers the BE move for leg 3. SILVER: clamped -> be_done must
    stay False so the retry keeps tightening."""
    t = _trader(tmp_path, {1: dict(LEG2, leg=1), 3: dict(LEG3)})
    sends = _fake_world(
        monkeypatch, t,
        positions=[_pos(3, "XAGUSD", 1, 64.398, 64.540)],
        deals_by_ticket={1: [_deal(169.28, entry=1, price=64.317)]},
    )

    t.check_and_move_breakeven()

    assert any(m.startswith("✅ [XAGUSD] TRADE CLOSED #1") for m in t.bot.messages)
    assert sends and sends[-1]["sl"] == 64.418  # BE attempt sent for leg 3
    assert not t.active_trades[3].get("be_done")  # clamped -> retry pending
    assert 3 in t.active_trades  # leg 3 still open


def test_close_detector_be_block_marks_done_on_real_acceptance(monkeypatch, tmp_path):
    """GOLD (no min distance): the BE move to entry + 1 tick is accepted, so
    be_done is set and no further retries are needed."""
    t = _trader(tmp_path, {1: dict(LEG2, symbol="XAUUSD", type="long", leg=1,
                                   entry_price=4393.03),
                           3: dict(LEG3, symbol="XAUUSD", type="long",
                                   entry_price=4393.03)})
    sends = _fake_world(
        monkeypatch, t,
        positions=[_pos(3, "XAUUSD", 0, 4393.03, 4388.00)],
        deals_by_ticket={1: [_deal(169.28, entry=1, price=4398.03)]},
    )

    t.check_and_move_breakeven()

    assert any(m.startswith("✅ [XAUUSD] TRADE CLOSED #1") for m in t.bot.messages)
    assert sends and sends[-1]["sl"] == 4393.04
    assert t.active_trades[3]["be_done"] is True
