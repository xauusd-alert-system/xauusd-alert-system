"""P1.5 demo MT5 TradeGroup execution tests (ТЗ §42–§46).

A deterministic ``FakeMT5`` double mirrors the REAL MetaTrader5 Python package
surface used by the adapters (account_info / symbol_info / symbol_info_tick /
order_send / positions_get / history_deals_get + constants), so the adapters
run unchanged on a Windows demo terminal. The double supports injected
rejections, partial fills and disconnects for the broker-failure scenarios
(ТЗ §44).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from data.trade_group_store import list_actions, load_group
from data.trading_event_ledger import read_trading_events
from execution.execution_intent import ExecutionIntent
from execution.mt5_trade_group import DemoAccountRequired, MT5TradeGroupExecutor
from execution.reconciliation import detect_orphan_positions
from execution.trade_geometry import BrokerSnapshot, CostSnapshot, GeometryRejected
from execution.trade_group import GroupState, TradeGroupSpec
from execution.trade_group_executor import LiveExecutionForbidden


# ==========================================================================
# Deterministic MT5 double (real API surface only)
# ==========================================================================

class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 1
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REQ_REJECT = 10002
    TRADE_RETCODE_INVALID_REQUEST = 10014
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1

    def __init__(self, account_mode="netting", trade_mode=0, balance=10000.0,
                 bid=99.9, ask=100.25):
        self.account_mode = account_mode
        self.trade_mode = trade_mode
        self.balance = balance
        self.positions: dict[int, dict] = {}
        self.deals: list[dict] = []
        # market price near the 100-scale spec entry; tests move it to TP levels
        self.bid = bid
        self.ask = ask
        self._pt = 100001
        self._ot = 1
        self._dt = 1
        # injection hooks
        self.inject_reject_open = False        # next DEAL open -> REQ_REJECT
        self.inject_reject_open_once = 0       # N opens rejected, then ok
        self.inject_reject_open_nth = None     # reject ONLY the Nth open
        self._open_count = 0
        self.inject_partial_open = None        # fraction (0..1) for next open
        self.inject_reject_modify = False
        self.inject_no_account = False

    # ---- API surface -----------------------------------------------------

    def initialize(self):
        return True

    def shutdown(self):
        return True

    def account_info(self):
        if self.inject_no_account:
            return None
        return SimpleNamespace(
            login=123456, balance=self.balance, equity=self.balance,
            currency="USD", trade_mode=self.trade_mode,
            margin_mode=(self.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
                         if self.account_mode == "hedging"
                         else self.ACCOUNT_MARGIN_MODE_RETAIL_NETTING),
        )

    def symbol_info(self, symbol):
        return SimpleNamespace(
            digits=2, point=0.01, trade_tick_size=0.01,
            trade_stops_level=0, trade_freeze_level=0,
            trade_contract_size=100.0, volume_min=0.01,
            volume_max=100.0, volume_step=0.01,
            trade_exec_mode="request",
        )

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=self.bid, ask=self.ask, time=0)

    def order_send(self, request):
        action = request.get("action")
        if action == self.TRADE_ACTION_SLTP:
            return self._modify(request)
        return self._deal(request)

    def positions_get(self, symbol=None, ticket=None):
        out = []
        for pos in self.positions.values():
            if ticket is not None and pos["ticket"] != ticket:
                continue
            if symbol is not None and pos["symbol"] != symbol:
                continue
            out.append(SimpleNamespace(**pos))
        return tuple(out) if out else None

    def history_deals_get(self, position=None):
        deals = self.deals
        if position is not None:
            deals = [d for d in deals if d["position_id"] == position]
        return tuple(SimpleNamespace(**d) for d in deals) if deals else None

    # ---- internals -------------------------------------------------------

    def _modify(self, request):
        ticket = int(request.get("position", 0))
        pos = self.positions.get(ticket)
        if pos is None:
            return SimpleNamespace(retcode=self.TRADE_RETCODE_INVALID_REQUEST,
                                   order=0, comment="No such position")
        if self.inject_reject_modify:
            return SimpleNamespace(retcode=self.TRADE_RETCODE_REQ_REJECT,
                                   order=0, comment="requote")
        pos["sl"] = float(request.get("sl", 0.0) or 0.0)
        pos["tp"] = float(request.get("tp", 0.0) or 0.0)
        self._ot += 1
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE,
                               order=self._ot - 1, deal=0, comment="modified")

    def _deal(self, request):
        position = int(request.get("position", 0) or 0)
        symbol = request.get("symbol", "")
        volume = float(request.get("volume", 0.0))
        order_type = int(request.get("type", 0))
        magic = int(request.get("magic", 0) or 0)
        comment = request.get("comment", "")
        t = 1_700_000_000 + len(self.deals)

        if position != 0:  # close (partial)
            pos = self.positions.get(position)
            if pos is None:
                return SimpleNamespace(retcode=self.TRADE_RETCODE_INVALID_REQUEST,
                                       order=0, comment="No such open position")
            price = float(request.get("price") or (self.bid if pos["type"] == 0 else self.ask))
            close_volume = min(volume, pos["volume"])
            self._dt += 1
            self.deals.append({
                "ticket": self._dt - 1, "position_id": position, "symbol": symbol,
                "type": order_type, "entry": self.DEAL_ENTRY_OUT, "price": price,
                "volume": close_volume, "magic": magic, "comment": comment, "time": t,
            })
            pos["volume"] = round(pos["volume"] - close_volume, 6)
            if pos["volume"] <= 1e-9:
                del self.positions[position]
            self._ot += 1
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE,
                                   order=self._ot - 1, deal=self._dt - 1,
                                   price=price, volume=close_volume, comment=comment)

        # open
        self._open_count += 1
        if self.inject_reject_open or self.inject_reject_open_once > 0 or (
                self.inject_reject_open_nth is not None
                and self._open_count == self.inject_reject_open_nth):
            if self.inject_reject_open_once > 0:
                self.inject_reject_open_once -= 1
            elif self.inject_reject_open:
                self.inject_reject_open = False
            return SimpleNamespace(retcode=self.TRADE_RETCODE_REQ_REJECT,
                                   order=0, comment="requote")

        step = 0.01
        volume = round(round(volume / step) * step, 2)
        if self.inject_partial_open is not None:
            volume = max(0.01, round(round(volume * self.inject_partial_open / step) * step, 2))
            self.inject_partial_open = None
        price = float(request.get("price") or (self.ask if order_type == 0 else self.bid))
        self._pt += 1
        self._dt += 1
        self._ot += 1
        ticket = self._pt - 1
        self.positions[ticket] = {
            "ticket": ticket, "symbol": symbol, "type": order_type,
            "volume": volume, "price_open": price, "price_current": price,
            "sl": float(request.get("sl", 0.0) or 0.0) or None,
            "tp": float(request.get("tp", 0.0) or 0.0) or None,
            "magic": magic, "comment": comment,
        }
        self.deals.append({
            "ticket": self._dt - 1, "position_id": ticket, "symbol": symbol,
            "type": order_type, "entry": self.DEAL_ENTRY_IN, "price": price,
            "volume": volume, "magic": magic, "comment": comment, "time": t,
        })
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE,
                               order=self._ot - 1, deal=self._dt - 1,
                               price=price, volume=volume, comment=comment)

    # ---- test helpers ----------------------------------------------------

    def broker_close(self, ticket: int, price: float) -> None:
        """Simulate a broker-side TP/SL closure (hedging: broker closes a leg)."""
        pos = self.positions.get(ticket)
        if pos is None:
            return
        self._dt += 1
        self.deals.append({
            "ticket": self._dt - 1, "position_id": ticket, "symbol": pos["symbol"],
            "type": 1 if pos["type"] == 0 else 0, "entry": self.DEAL_ENTRY_OUT,
            "price": price, "volume": pos["volume"],
            "magic": pos["magic"], "comment": "broker tp/sl", "time": 1_700_000_100,
        })
        del self.positions[ticket]

    def open_manual_position(self, ticket: int, symbol="GOLD", comment="TG:UNKNOWN|L:1|777111") -> None:
        self.positions[ticket] = {
            "ticket": ticket, "symbol": symbol, "type": 0, "volume": 0.01,
            "price_open": 100.0, "price_current": 100.0, "sl": 90.0, "tp": 104.0,
            "magic": 777111, "comment": comment,
        }


# ==========================================================================
# Fixtures
# ==========================================================================

COST = CostSnapshot(round_trip_cost_price=0.30, safety_buffer_price=0.10,
                    expected_exit_slippage=0.10, commission_buffer=0.05)


def make_spec(side="long", mode="demo", group_id="TG-DEMO-1", total_volume=0.03) -> TradeGroupSpec:
    if side == "long":
        entry, tp1, tp2, tp3, sl = 100.0, 104.0, 108.0, 112.0, 90.0
    else:
        entry, tp1, tp2, tp3, sl = 100.0, 96.0, 92.0, 88.0, 110.0
    return TradeGroupSpec(
        group_id=group_id, signal_id="SGL-DEMO-1", intent_id="INT-DEMO-1",
        asset_key="XAUUSD", broker_symbol="GOLD", mode=mode, side=side,
        entry={"low": 99.0, "high": 101.0, "reference": entry},
        geometry={"version": "demo_v1", "unit": "price", "step_price": 4.0,
                  "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl},
        targets=[{"leg": 1, "price": tp1, "allocation": 1 / 3},
                 {"leg": 2, "price": tp2, "allocation": 1 / 3},
                 {"leg": 3, "price": tp3, "allocation": 1 / 3}],
        break_even={"trigger": "tp1_filled",
                    "raw_price_policy": "actual_fill",
                    "protected_price_policy": "actual_fill_plus_cost_buffer",
                    "apply_to": [2, 3]},
        risk={"currency": "USD", "max_cash": 50.0, "max_pct": 0.5,
              "estimated_loss_at_sl": 30.0, "total_volume": total_volume},
        profile_id="demo_v1", model_version="v3", model_hash="m" * 64,
        config_hash="c" * 64, strategy_version="s3",
        expires_at_utc_ms=1_900_000_000_000, created_at_utc_ms=1_700_000_000_000,
    )


def make_executor(tmp_path, mt5, **kwargs):
    messages: list[str] = []
    executor = MT5TradeGroupExecutor(
        str(tmp_path / "groups.sqlite"), mt5=mt5, allow_demo=True,
        notifier=messages.append, cost=COST, magic=777111, **kwargs,
    )
    return executor, messages


def _events(db) -> list[str]:
    df = read_trading_events(db)
    return [r["event_type"] for r in df.to_dict("records")]


def _positions_by_leg(mt5, group_id: str) -> dict[int, dict]:
    out = {}
    for ticket, pos in mt5.positions.items():
        comment = str(pos.get("comment", "") or "")
        for leg in (1, 2, 3):
            if f"L:{group_id}-L{leg}" in comment:
                out[leg] = pos
    return out


# ==========================================================================
# §46 tests
# ==========================================================================

def test_real_demo_account_gate(tmp_path):
    real = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
    executor, _ = make_executor(tmp_path, real)
    executor.create_group(make_spec(mode="demo"))
    with pytest.raises(DemoAccountRequired):
        executor.submit_group("TG-DEMO-1")
    demo = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_DEMO)
    executor2, _ = make_executor(tmp_path, demo)
    executor2.create_group(make_spec(mode="demo", group_id="TG-DEMO-2"))
    assert executor2.submit_group("TG-DEMO-2") == GroupState.SUBMITTED


def test_live_always_forbidden(tmp_path):
    executor, _ = make_executor(tmp_path, FakeMT5())
    with pytest.raises(LiveExecutionForbidden):
        executor.create_group(make_spec(mode="live"))


def test_hedging_submit_three_legs_long(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(side="long")
    executor.create_group(spec)
    assert executor.submit_group(spec.group_id) == GroupState.SUBMITTED
    assert len(mt5.positions) == 3
    by_leg = _positions_by_leg(mt5, spec.group_id)
    assert set(by_leg) == {1, 2, 3}
    assert by_leg[1]["tp"] == 104.0 and by_leg[2]["tp"] == 108.0 and by_leg[3]["tp"] == 112.0
    for leg, pos in by_leg.items():
        assert pos["sl"] == 90.0
        assert pos["magic"] == 777111
        assert "TG:" in pos["comment"]
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.SUBMITTED
    assert stored["account_mode"] == "hedging"


def test_hedging_submit_three_legs_short(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(side="short")
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    by_leg = _positions_by_leg(mt5, spec.group_id)
    assert set(by_leg) == {1, 2, 3}
    assert by_leg[1]["tp"] == 96.0 and by_leg[2]["tp"] == 92.0 and by_leg[3]["tp"] == 88.0
    assert all(pos["sl"] == 110.0 for pos in by_leg.values())
    assert all(pos["type"] == FakeMT5.ORDER_TYPE_SELL for pos in by_leg.values())


def test_netting_submit_virtual_legs(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(side="long")
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    assert len(mt5.positions) == 1                       # 1 aggregate position
    aggregate = list(mt5.positions.values())[0]
    assert aggregate["sl"] == 90.0
    assert aggregate["tp"] == 112.0                      # final target
    stored = load_group(executor.db_path, spec.group_id)
    leg_states = {item["leg"]: item["state"] for item in stored["legs"]}
    assert leg_states[2] == "VIRTUAL" and leg_states[3] == "VIRTUAL"


def test_actual_fill_persisted(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.OPENED
    assert stored["spec"].entry.actual_fill == 100.25    # ask price at open


def test_tp1_broker_confirmation(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, messages = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                 # OPENED
    assert "TRADE GROUP OPENED" in messages[0]
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 104.0)         # broker TP closes leg1
    events = executor.poll_once()
    assert "tp1_filled" in events
    stored = load_group(executor.db_path, spec.group_id)
    # TP1 + BE complete in one pass when the broker accepts the BE modify
    assert stored["state"] in (GroupState.TP1_FILLED, GroupState.BE_CONFIRMED)
    leg1 = next(i for i in stored["legs"] if i["leg"] == 1)
    assert leg1["state"] == "CLOSED"
    assert stored["spec"].entry.actual_fill is not None


def test_be_all_remaining_legs_hedging(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 104.0)
    events = executor.poll_once()                        # TP1 + BE verify (one pass)
    assert "be_confirmed" in events
    stored = load_group(executor.db_path, spec.group_id)
    be = stored["be_state"]["confirmed_price"]
    for leg in (2, 3):
        pos = by_leg[leg]
        assert pos["sl"] == be                           # broker query confirms
    assert be > 100.0                                    # long: protected above fill


def test_be_long_direction(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(side="long")
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                 # OPENED (fill ~4159.55)
    mt5.bid = mt5.ask = 104.0                            # candidate at TP1
    executor.poll_once()                                 # TP1 + BE_REQUESTED
    executor.poll_once()                                 # BE verify
    stored = load_group(executor.db_path, spec.group_id)
    be = stored["be_state"]["confirmed_price"]
    fill = stored["spec"].entry.actual_fill
    assert be > fill                                     # protected BE above raw


def test_be_short_direction(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(side="short")
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                 # OPENED
    mt5.bid = mt5.ask = 96.0                             # candidate at TP1 (short)
    executor.poll_once()
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    be = stored["be_state"]["confirmed_price"]
    fill = stored["spec"].entry.actual_fill
    assert be < fill                                     # protected BE below raw


def test_be_rejection_retry(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    mt5.bid = mt5.ask = 104.0
    mt5.inject_reject_modify = True                      # reject BEFORE the attempt
    events = executor.poll_once()                        # TP1 + BE_REQUESTED + retry
    assert "be_retry" in events
    assert "be_confirmed" not in _events(executor.ledger_db_path)
    mt5.inject_reject_modify = False
    events = executor.poll_once()
    assert "be_confirmed" in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.BE_CONFIRMED
    assert stored["be_state"]["retries"] == 1


def test_tp2_tp3_immutable_after_tp1(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 104.0)
    executor.poll_once()
    executor.poll_once()                                 # BE_CONFIRMED
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["spec"].geometry.tp2 == 108.0
    assert stored["spec"].geometry.tp3 == 112.0
    # broker closes leg2 at TP2 -> TP2_FILLED; levels still immutable
    mt5.broker_close(by_leg[2]["ticket"], 108.0)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.TP2_FILLED
    assert stored["spec"].geometry.tp2 == 108.0
    assert stored["spec"].geometry.tp3 == 112.0


def test_stop_before_tp1(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, messages = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                 # OPENED
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 90.0)          # SL hit
    events = executor.poll_once()
    assert "stop_filled" in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.STOPPED
    assert "be_requested" not in _events(executor.ledger_db_path)
    assert "🛑 STOPPED" in messages[-1]


def test_partial_fill_hedging(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)                  # legs 0.02/0.02/0.02
    executor.create_group(spec)
    mt5.inject_partial_open = 0.5                        # leg1 fills 0.01 of 0.02
    executor.submit_group(spec.group_id)
    assert "leg_partially_filled" in _events(executor.ledger_db_path)
    stored = load_group(executor.db_path, spec.group_id)
    leg1 = next(i for i in stored["legs"] if i["leg"] == 1)
    assert leg1["state"] == "PARTIALLY_FILLED"
    assert leg1["broker"]["filled_volume"] == 0.01
    # deterministic recovery policy: remainder is cancelled (never re-ordered)
    executor.poll_once()                                 # OPENED
    assert len(mt5.positions) == 3                       # no extra top-up order
    assert sum(1 for _ in mt5.deals) == 3                # exactly 3 open deals
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.OPENED


def test_order_reject_deterministic_final_state(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    mt5.inject_reject_open_nth = 3                       # leg3 rejected only
    executor.submit_group(spec.group_id)
    stored = load_group(executor.db_path, spec.group_id)
    leg3 = next(i for i in stored["legs"] if i["leg"] == 3)
    assert leg3["state"] == "REJECTED"
    assert "leg_rejected" in _events(executor.ledger_db_path)
    assert stored["state"] == GroupState.SUBMITTED
    # controlled recovery: poll detects the incomplete submission -> FAILED,
    # while the accepted legs keep their broker ids (no silent orphans)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert "execution_error" in _events(executor.ledger_db_path)
    assert len(mt5.positions) == 2


def test_restart_after_open(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                 # OPENED
    order_count = mt5._ot
    executor2, _ = make_executor(tmp_path, mt5)          # "restart", same broker
    restored = executor2.recover_after_restart(spec.group_id)
    assert restored["state"] == GroupState.OPENED
    assert restored["spec"].geometry.tp1 == 104.0
    executor2.poll_once()
    assert mt5._ot == order_count                        # no duplicate orders


def test_restart_after_tp1(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 104.0)
    executor.poll_once()                                 # TP1 + BE (one pass)
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.BE_CONFIRMED
    assert stored["be_state"]["status"] == "BE_CONFIRMED"
    order_count = mt5._ot
    executor2, _ = make_executor(tmp_path, mt5)          # restart after TP1+BE
    restored = executor2.recover_after_restart(spec.group_id)
    assert restored["state"] == GroupState.BE_CONFIRMED
    assert restored["spec"].geometry.tp2 == 108.0        # remaining legs/TP2/TP3
    assert restored["spec"].geometry.tp3 == 112.0
    executor2.poll_once()
    events = _events(executor2.ledger_db_path)
    assert events.count("tp1_filled") == 1               # no duplicate TP1
    assert events.count("be_confirmed") == 1             # no duplicate BE
    assert mt5._ot == order_count                        # no re-sent orders


def test_restart_during_be_retry(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    mt5.bid = mt5.ask = 104.0
    mt5.inject_reject_modify = True                      # reject BEFORE the attempt
    executor.poll_once()                                 # TP1 + BE_REQUESTED + retry
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.BE_RETRY
    executor2, _ = make_executor(tmp_path, mt5)          # restart mid-retry
    restored = executor2.recover_after_restart(spec.group_id)
    assert restored["state"] == GroupState.BE_RETRY
    assert restored["be_state"]["status"] == "BE_RETRY"
    mt5.inject_reject_modify = False
    executor2.poll_once()                                # retry succeeds
    stored = load_group(executor2.db_path, spec.group_id)
    assert stored["state"] == GroupState.BE_CONFIRMED
    assert stored["be_state"]["status"] == "BE_CONFIRMED"


def test_idempotent_action(tmp_path):
    from data.trade_group_store import mark_action
    db = str(tmp_path / "actions.sqlite")
    assert mark_action(db, "TG-1", "OPEN-L1") is True
    assert mark_action(db, "TG-1", "OPEN-L1") is False  # duplicate blocked
    assert mark_action(db, "TG-1", "OPEN-L2") is True
    actions = list_actions(db, "TG-1")
    assert {a["action_id"] for a in actions} == {"OPEN-L1", "OPEN-L2"}


def test_orphan_position_detection(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    mt5.open_manual_position(777001, comment="TG:UNKNOWN|L:1|777111")
    spec = make_spec()
    executor.create_group(spec)
    driver = executor._resolve_driver(spec)
    orphans = detect_orphan_positions(driver, executor.db_path,
                                      ledger_db_path=executor.ledger_db_path)
    assert len(orphans) == 1
    assert orphans[0]["ticket"] == 777001
    assert "orphan_broker_position" in _events(executor.ledger_db_path)
    # idempotent: second scan does not re-emit
    orphans2 = detect_orphan_positions(driver, executor.db_path,
                                       ledger_db_path=executor.ledger_db_path)
    assert len(orphans2) == 1
    assert _events(executor.ledger_db_path).count("orphan_broker_position") == 1


def test_no_duplicate_order_after_reconciliation(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    for _ in range(5):
        executor.poll_once()
    order_count = mt5._ot
    executor.poll_once()
    executor.poll_once()
    assert mt5._ot == order_count


def test_telegram_opened_after_confirmation(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, messages = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    assert messages == []                                # no message before confirmation
    executor.poll_once()                                 # OPENED confirmed
    assert any("TRADE GROUP OPENED" in m for m in messages)
    opened = next(m for m in messages if "TRADE GROUP OPENED" in m)
    assert "Group: TG-DEMO-1" in opened
    assert "Mode: DEMO" in opened
    assert "Entry: 100.25" in opened                     # actual fill, not reference


def test_telegram_tp1_after_confirmation(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, messages = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    mt5.bid = mt5.ask = 104.0
    assert not any("TP1 FILLED" in m for m in messages)  # candidate is not enough
    executor.poll_once()                                 # confirmed close
    assert any("✅ TP1 FILLED" in m for m in messages)


def test_telegram_be_after_confirmation(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, messages = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    mt5.bid = mt5.ask = 104.0
    mt5.inject_reject_modify = True                      # reject BEFORE the attempt
    executor.poll_once()                                 # TP1 + BE_REQUESTED + retry
    assert not any("BE CONFIRMED" in m for m in messages)
    assert "be_retry" in _events(executor.ledger_db_path)
    mt5.inject_reject_modify = False
    executor.poll_once()                                 # retry -> BE_CONFIRMED
    assert any("🟢 BE CONFIRMED" in m for m in messages)


def test_btc_unvalidated_profile_blocked(tmp_path):
    from execution.trade_geometry import build_trade_group_from_signal
    from execution.trade_geometry import PROFILE_NOT_VALIDATED
    cfg = {"trade_profiles": {"btc_m5_scalp_v1": {
        "asset": "BTCUSD", "validated": False, "paper_only": True,
        "step": {"source": "atr", "atr_mult": 1.0,
                 "min_price_distance": 4.0, "max_price_distance": 6.0},
        "targets": {"multipliers": {"tp1": 1.0, "tp2": 2.0, "tp3": 3.0}},
        "stop": {"multiplier": 2.0},
        "allocation": {"tp1": 1 / 3, "tp2": 1 / 3, "tp3": 1 / 3},
        "risk": {"max_cash": 25.0, "max_pct": 0.5}, "volume": {"total": 0.01},
    }}}
    signal = {"bias": "long", "atr": 5.0, "entry_zone": [60000.0, 60010.0],
              "expires_at_utc_ms": 1_900_000_000_000}
    with pytest.raises(GeometryRejected) as exc:
        build_trade_group_from_signal(
            signal, cfg=cfg, asset_key="BTCUSD", profile_id="btc_m5_scalp_v1",
            broker=BrokerSnapshot(symbol_point=0.01, tick_size=0.01,
                                  spread=5.0, contract_size=1.0,
                                  volume_step=0.001, volume_min=0.001,
                                  balance=10000.0),
            cost=COST, mode="demo", now_ms=1_700_000_000_000,
        )
    assert exc.value.reason_code == PROFILE_NOT_VALIDATED


def test_execution_intent_geometry_verification(tmp_path):
    spec = make_spec()
    intent = ExecutionIntent.from_spec(spec)
    assert intent.verify_geometry(spec) is True
    # any drift in the approved spec must fail the intent check
    drifted = spec.model_copy(update={"geometry": spec.geometry.model_copy(
        update={"tp1": 105.0})})
    assert intent.verify_geometry(drifted) is False
    with pytest.raises(Exception, match="geometry_hash"):
        intent.require_geometry_unchanged(drifted)


def test_broker_unavailable_no_false_success(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    mt5.inject_no_account = True
    with pytest.raises(Exception):
        executor.submit_group(spec.group_id)
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] in (GroupState.VALIDATED, GroupState.REJECTED)
    assert len(mt5.positions) == 0                       # no false success


def test_netting_full_cycle_scenario_c(tmp_path):
    """ТЗ §42 Scenario C: netting LONG — TP1 partial close, BE, TP2, TP3."""
    mt5 = FakeMT5(account_mode="netting")
    executor, messages = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)                  # legs 0.02/0.02/0.02
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                 # OPENED (1 position)
    assert len(mt5.positions) == 1
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                 # TP1 partial close
    executor.poll_once()                                 # BE
    assert list(mt5.positions.values())[0]["volume"] == pytest.approx(0.04)
    mt5.bid = mt5.ask = 108.0
    executor.poll_once()                                 # TP2 partial close
    assert list(mt5.positions.values())[0]["volume"] == pytest.approx(0.02)
    mt5.bid = mt5.ask = 112.0
    executor.poll_once()                                 # TP3 final close
    assert mt5.positions == {}
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.RECONCILED
    events = _events(executor.ledger_db_path)
    for event in ("tp1_filled", "be_confirmed", "tp2_filled", "tp3_filled",
                  "group_reconciled"):
        assert events.count(event) == 1, event
    assert any("✅ TP3 FILLED" in m for m in messages)


def test_hedging_full_cycle_scenario_a(tmp_path):
    """ТЗ §42 Scenario A: hedging LONG — broker TP closes each leg."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                 # OPENED
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 104.0)
    executor.poll_once()                                 # TP1
    executor.poll_once()                                 # BE_CONFIRMED
    mt5.broker_close(by_leg[2]["ticket"], 108.0)
    executor.poll_once()                                 # TP2
    mt5.broker_close(by_leg[3]["ticket"], 112.0)
    executor.poll_once()                                 # TP3 + RECONCILED
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.RECONCILED
    events = _events(executor.ledger_db_path)
    assert events.count("tp1_filled") == 1
    assert events.count("be_confirmed") == 1
    assert events.count("tp2_filled") == 1
    assert events.count("tp3_filled") == 1
    assert events.count("group_reconciled") == 1


def test_hedging_short_full_cycle(tmp_path):
    """ТЗ §42 Scenario B: hedging SHORT mirror."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(side="short")
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 96.0)
    executor.poll_once()
    executor.poll_once()                                 # BE_CONFIRMED
    mt5.broker_close(by_leg[2]["ticket"], 92.0)
    executor.poll_once()
    mt5.broker_close(by_leg[3]["ticket"], 88.0)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.RECONCILED


def test_be_retries_exhausted_fails(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5, max_be_retries=2)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    mt5.bid = mt5.ask = 104.0
    mt5.inject_reject_modify = True                      # reject BEFORE the attempt
    executor.poll_once()                                 # TP1 + BE_REQUESTED + retry 1
    executor.poll_once()                                 # retry 2 -> exhausted -> FAILED
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert "be_confirmed" not in _events(executor.ledger_db_path)


def test_tp1_closes_leg1(tmp_path):
    """ТЗ §46: after a confirmed TP1 the leg-1 state is CLOSED."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 104.0)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    leg1 = next(i for i in stored["legs"] if i["leg"] == 1)
    assert leg1["state"] == "CLOSED"
    assert leg1["fill_price"] == 104.0


def test_tp2_immutable_after_tp1(tmp_path):
    """ТЗ §46/§24: TP2 after TP1 == TP2 before TP1 (state + broker request)."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    tp2_before = spec.geometry.tp2
    executor.poll_once()
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 104.0)
    executor.poll_once()                                 # TP1 + BE
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["spec"].geometry.tp2 == tp2_before == 108.0
    # leg2's broker TP was never modified by BE (only SL moves)
    assert by_leg[2]["tp"] == 108.0


def test_tp3_immutable_after_tp1(tmp_path):
    """ТЗ §46/§24: TP3 after TP1 == TP3 before TP1 (state + broker request)."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    tp3_before = spec.geometry.tp3
    executor.poll_once()
    by_leg = _positions_by_leg(mt5, spec.group_id)
    mt5.broker_close(by_leg[1]["ticket"], 104.0)
    executor.poll_once()                                 # TP1 + BE
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["spec"].geometry.tp3 == tp3_before == 112.0
    assert by_leg[3]["tp"] == 112.0
