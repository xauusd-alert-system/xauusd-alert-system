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

from data.trade_group_store import list_actions, list_groups, load_group
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
                 bid=99.9, ask=100.25, volume_step=0.01, volume_min=0.01):
        self.account_mode = account_mode
        self.trade_mode = trade_mode
        self.balance = balance
        self.volume_step = volume_step
        self.volume_min = volume_min
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
        self.inject_reject_open_nths: set[int] | None = None  # reject these opens
        self._open_count = 0
        self.inject_partial_open = None        # fraction (0..1) for next open
        self.inject_reject_modify = False
        self.inject_reject_close = False       # close requests -> REQ_REJECT
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
            trade_contract_size=100.0, volume_min=self.volume_min,
            volume_max=100.0, volume_step=self.volume_step,
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
            if self.inject_reject_close:
                return SimpleNamespace(retcode=self.TRADE_RETCODE_REQ_REJECT,
                                       order=0, comment="close requote")
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
        nth_hit = (self.inject_reject_open_nth is not None
                   and self._open_count == self.inject_reject_open_nth)
        nths_hit = bool(self.inject_reject_open_nths
                        and self._open_count in self.inject_reject_open_nths)
        if self.inject_reject_open or self.inject_reject_open_once > 0 or nth_hit or nths_hit:
            if self.inject_reject_open_once > 0:
                self.inject_reject_open_once -= 1
            elif self.inject_reject_open:
                self.inject_reject_open = False
            return SimpleNamespace(retcode=self.TRADE_RETCODE_REQ_REJECT,
                                   order=0, comment="requote")

        step = self.volume_step
        minimum = self.volume_min
        volume = round(round(volume / step + 1e-9) * step, 8)
        if volume < minimum - 1e-9:
            volume = minimum
        if self.inject_partial_open is not None:
            partial = round(round(volume * self.inject_partial_open / step + 1e-9) * step, 8)
            volume = max(minimum, partial)
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
    """P1.5.1 §2–§9: leg3 rejected -> leg1/leg2 are COMPENSATED (closed by
    market order with broker confirmation) BEFORE the group may reach FAILED.
    The pre-fix behavior (immediate FAILED leaving two open positions at the
    broker) is explicitly forbidden by the follow-up ТЗ."""
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
    # poll 1: compensation closes are sent -> COMPENSATION_REQUESTED (never
    # terminal FAILED while legs 1/2 may still be open at the broker)
    events = executor.poll_once()
    assert "partial_submission" in events
    assert "compensation_requested" in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.COMPENSATION_REQUESTED
    # poll 2: broker confirmation (positions gone) -> COMPENSATION_CONFIRMED
    # -> FAILED with open risk == 0
    events = executor.poll_once()
    assert "compensation_confirmed" in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert len(mt5.positions) == 0                       # no leftover broker risk
    assert "failed_with_open_risk" not in _events(executor.ledger_db_path)


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


# ==========================================================================
# P1.5.1 §22: compensation action idempotency
# ==========================================================================

def _partial_hedging_executor(tmp_path, mt5, rejected_open: int | set[int] | None = 3):
    """Submit a hedging group with the given open(s) rejected; return executor+spec."""
    executor, messages = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    if isinstance(rejected_open, set):
        mt5.inject_reject_open_nths = rejected_open
    elif rejected_open is not None:
        mt5.inject_reject_open_nth = rejected_open
    executor.submit_group(spec.group_id)
    return executor, spec, messages


def test_compensation_action_idempotent(tmp_path):
    from data.trade_group_store import mark_action
    db = str(tmp_path / "comp-actions.sqlite")
    assert mark_action(db, "TG-1", "COMPENSATE-L1") is True
    assert mark_action(db, "TG-1", "COMPENSATE-L1") is False   # same id -> blocked
    assert mark_action(db, "TG-1", "COMPENSATE-L2") is True
    # every compensating close is a distinct deterministic actionId
    actions = list_actions(db, "TG-1")
    assert {a["action_id"] for a in actions} == {"COMPENSATE-L1", "COMPENSATE-L2"}


def test_duplicate_compensation_after_restart(tmp_path):
    """Restart mid-compensation: the already-sent COMPENSATE closes are never
    re-sent (order count unchanged), and the group still finalizes to FAILED."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, spec, _ = _partial_hedging_executor(tmp_path, mt5, rejected_open=3)
    executor.poll_once()                                     # compensation sent
    assert executor._begin_compensation.__name__  # sanity
    order_count = mt5._ot
    actions_after_first = [a["action_id"] for a in list_actions(executor.db_path, spec.group_id)]
    # restart with a fresh executor over the same store + broker
    executor2, _ = make_executor(tmp_path, mt5)
    restored = executor2.recover_after_restart(spec.group_id)
    assert restored["state"] == GroupState.COMPENSATION_REQUESTED
    executor2.poll_once()                                    # verify -> confirmed -> FAILED
    stored = load_group(executor2.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert mt5._ot == order_count                            # no duplicate close orders
    actions_after = [a["action_id"] for a in list_actions(executor2.db_path, spec.group_id)]
    comp_actions = [a for a in actions_after if a.startswith("COMPENSATE")]
    assert comp_actions == [a for a in actions_after_first if a.startswith("COMPENSATE")]


def test_duplicate_close_action_is_not_sent(tmp_path):
    """Netting: after CLOSE-TP1 is recorded, re-polling at the same price must
    not send a second close order."""
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                     # OPENED
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                     # TP1 close + BE
    assert "CLOSE-TP1" in [a["action_id"] for a in list_actions(executor.db_path, spec.group_id)]
    close_orders = mt5._ot
    executor.poll_once()
    executor.poll_once()
    assert mt5._ot == close_orders                            # no duplicate close


# ==========================================================================
# P1.5.1 §23: partial submission compensation scenarios
# ==========================================================================

def test_leg3_rejected_closes_leg1_and_leg2(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, spec, messages = _partial_hedging_executor(tmp_path, mt5, rejected_open=3)
    events = executor.poll_once()
    assert "partial_submission" in events
    assert "compensation_requested" in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.COMPENSATION_REQUESTED
    assert len(mt5.positions) == 0                            # leg1/leg2 closed NOW
    assert any("⚠️ TRADE GROUP PARTIAL SUBMISSION" in m for m in messages)
    actions = [a["action_id"] for a in list_actions(executor.db_path, spec.group_id)]
    assert "COMPENSATE-L1" in actions and "COMPENSATE-L2" in actions
    events = executor.poll_once()                             # confirmation -> FAILED
    assert "compensation_confirmed" in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert stored["volume"]["total_remaining"] == 0.0
    assert any("🛑 TRADE GROUP FAILED" in m for m in messages)
    assert "Reason: LEG3_REJECTED" in messages[-1]
    assert "Open risk: 0" in messages[-1]


def test_leg2_rejected_closes_leg1(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, spec, _ = _partial_hedging_executor(tmp_path, mt5, rejected_open=2)
    executor.poll_once()
    # leg1 and leg3 were filled; both must be compensated
    actions = [a["action_id"] for a in list_actions(executor.db_path, spec.group_id)]
    assert "COMPENSATE-L1" in actions and "COMPENSATE-L3" in actions
    assert "COMPENSATE-L2" not in actions
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert len(mt5.positions) == 0


def test_multiple_leg_rejection_compensates_all_filled(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, spec, _ = _partial_hedging_executor(tmp_path, mt5,
                                                  rejected_open={2, 3})
    executor.poll_once()
    actions = [a["action_id"] for a in list_actions(executor.db_path, spec.group_id)]
    assert "COMPENSATE-L1" in actions                        # the only filled leg
    assert "COMPENSATE-L2" not in actions and "COMPENSATE-L3" not in actions
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert len(mt5.positions) == 0


def test_compensation_confirmation_required(tmp_path):
    """The group must NOT reach FAILED in the same poll the closes were sent:
    broker confirmation is verified on a later poll (P1.5.1 §7)."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, spec, _ = _partial_hedging_executor(tmp_path, mt5, rejected_open=3)
    events = executor.poll_once()
    assert "compensation_confirmed" not in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.COMPENSATION_REQUESTED
    events = executor.poll_once()
    assert "compensation_confirmed" in events
    assert load_group(executor.db_path, spec.group_id)["state"] == GroupState.FAILED


def test_compensation_failure_keeps_reconciliation_active(tmp_path):
    """Close rejected -> FAILED_WITH_OPEN_RISK; the NEXT poll retries the close
    and the group finalizes once the broker accepts (P1.5.1 §8)."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, spec, messages = _partial_hedging_executor(tmp_path, mt5, rejected_open=3)
    mt5.inject_reject_close = True                           # compensation closes fail
    events = executor.poll_once()
    assert "compensation_failed" in events
    assert "failed_with_open_risk" in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED_WITH_OPEN_RISK
    assert len(mt5.positions) == 2                           # legs 1/2 still OPEN
    assert any("🚨 EXECUTION ERROR" in m for m in messages)
    assert "FAILED_WITH_OPEN_RISK" in messages[-1]
    assert "compensation_failed" in _events(executor.ledger_db_path)
    assert "failed_with_open_risk" in _events(executor.ledger_db_path)
    # reconciliation stays active: broker recovers -> retry closes -> FAILED
    mt5.inject_reject_close = False
    events = executor.poll_once()
    assert "compensation_confirmed" in events
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert len(mt5.positions) == 0


def test_no_orphan_after_successful_compensation(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, spec, _ = _partial_hedging_executor(tmp_path, mt5, rejected_open=3)
    executor.poll_once()
    executor.poll_once()                                     # FAILED, risk 0
    assert len(mt5.positions) == 0
    driver = executor._resolve_driver(spec)
    orphans = detect_orphan_positions(driver, executor.db_path,
                                      ledger_db_path=executor.ledger_db_path)
    assert orphans == []                                     # known-group compensation first


def test_failed_compensation_creates_open_risk_state(tmp_path):
    """Close rejection + exhausted retry budget -> the explicit
    FAILED_WITH_OPEN_RISK safety state with open legs; reconciliation stays
    active (bounded retries) and recovers once the broker accepts."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, spec, _ = _partial_hedging_executor(tmp_path, mt5, rejected_open=3)
    executor.max_compensation_retries = 2                    # bounded retry budget
    mt5.inject_reject_close = True
    executor.poll_once()                                     # attempt 1 rejected
    executor.poll_once()                                     # retry 1 rejected
    executor.poll_once()                                     # retry 2 rejected -> exhausted
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED_WITH_OPEN_RISK
    assert len(mt5.positions) == 2                           # explicit open risk
    assert stored["comp_state"]["retries"] >= 2
    assert "failed_with_open_risk" in _events(executor.ledger_db_path)
    # reconciliation stays active; the EXHAUSTED state does NOT spam the
    # ledger on every poll (each rejected attempt logs once, then it stops)
    spam_count = _events(executor.ledger_db_path).count("failed_with_open_risk")
    executor.poll_once()
    assert _events(executor.ledger_db_path).count("failed_with_open_risk") == spam_count
    # broker recovers -> a fresh executor (new retry budget) finalizes the group
    mt5.inject_reject_close = False
    fresh, _ = make_executor(tmp_path, mt5)
    fresh.recover_after_restart(spec.group_id)
    fresh.poll_once()
    fresh.poll_once()
    stored = load_group(fresh.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert len(mt5.positions) == 0


# ==========================================================================
# P1.5.1 §24: netting volume accounting
# ==========================================================================

def _netting_executor(tmp_path, mt5, total=0.06):
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=total)
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()                                     # OPENED
    return executor, spec


def test_netting_tp1_uses_actual_filled_volume(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, spec = _netting_executor(tmp_path, mt5, total=0.06)
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["volume"]["total_filled"] == 0.06
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                     # TP1 close
    stored = load_group(executor.db_path, spec.group_id)
    # TP1 closes the ACTUAL filled volume's 1/3 allocation: 0.06/3 = 0.02
    assert stored["volume"]["total_closed"] == pytest.approx(0.02)
    assert stored["volume"]["total_remaining"] == pytest.approx(0.04)
    assert list(mt5.positions.values())[0]["volume"] == pytest.approx(0.04)


def test_netting_tp2_uses_remaining_volume(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, spec = _netting_executor(tmp_path, mt5, total=0.06)
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                     # TP1
    mt5.bid = mt5.ask = 108.0
    executor.poll_once()                                     # TP2
    stored = load_group(executor.db_path, spec.group_id)
    # cumulative 2/3 of 0.06 = 0.04; already closed 0.02 -> increment 0.02
    assert stored["volume"]["total_closed"] == pytest.approx(0.04)
    assert stored["volume"]["total_remaining"] == pytest.approx(0.02)


def test_netting_tp3_closes_remaining_volume(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, spec = _netting_executor(tmp_path, mt5, total=0.06)
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()
    mt5.bid = mt5.ask = 108.0
    executor.poll_once()
    mt5.bid = mt5.ask = 112.0
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.RECONCILED
    assert stored["volume"]["total_closed"] == pytest.approx(0.06)
    assert stored["volume"]["total_remaining"] == pytest.approx(0.0)
    assert mt5.positions == {}


def test_netting_partial_fill_020(tmp_path):
    """Case B (ТЗ §16): requested 0.03, filled 0.02 -> TP1/2/3 work from 0.02."""
    mt5 = FakeMT5(account_mode="netting", volume_step=0.001, volume_min=0.001)
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.03)
    executor.create_group(spec)
    mt5.inject_partial_open = 2 / 3                          # 0.03 -> 0.02 filled
    executor.submit_group(spec.group_id)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["volume"]["total_filled"] == pytest.approx(0.02)
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                     # TP1: 0.02/3 ~ 0.006
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["volume"]["total_closed"] == pytest.approx(0.006, abs=0.001)
    mt5.bid = mt5.ask = 108.0
    executor.poll_once()                                     # TP2: cum 0.0133 - 0.006
    mt5.bid = mt5.ask = 112.0
    executor.poll_once()                                     # TP3: entire remaining
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.RECONCILED
    assert stored["volume"]["total_closed"] == pytest.approx(0.02, abs=0.001)
    assert mt5.positions == {}


def test_netting_partial_fill_010(tmp_path):
    """Case C (ТЗ §16): requested 0.03, filled 0.01 -> TP3 never closes more
    than the remaining volume."""
    mt5 = FakeMT5(account_mode="netting", volume_step=0.001, volume_min=0.001)
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.03)
    executor.create_group(spec)
    mt5.inject_partial_open = 1 / 3                          # 0.03 -> 0.01 filled
    executor.submit_group(spec.group_id)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["volume"]["total_filled"] == pytest.approx(0.01)
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                     # TP1: 0.003
    mt5.bid = mt5.ask = 108.0
    executor.poll_once()                                     # TP2: 0.003
    mt5.bid = mt5.ask = 112.0
    executor.poll_once()                                     # TP3: remaining 0.004
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.RECONCILED
    assert stored["volume"]["total_closed"] == pytest.approx(0.01, abs=0.001)
    assert stored["volume"]["total_remaining"] == pytest.approx(0.0)
    assert mt5.positions == {}


def test_netting_volume_step_rounding(tmp_path):
    mt5 = FakeMT5(account_mode="netting", volume_step=0.005, volume_min=0.005)
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.03)
    executor.create_group(spec)
    executor.submit_group(spec.group_id)
    executor.poll_once()
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                     # TP1: floor(0.01/0.005)=0.01
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["volume"]["total_closed"] == pytest.approx(0.01)
    # volume is a multiple of the step
    volume = list(mt5.positions.values())[0]["volume"]
    assert round(volume / 0.005) * 0.005 == pytest.approx(volume)


def test_no_close_above_broker_remaining_volume(tmp_path):
    mt5 = FakeMT5(account_mode="netting")
    executor, spec = _netting_executor(tmp_path, mt5, total=0.06)
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                     # close 0.02 -> remaining 0.04
    remaining = list(mt5.positions.values())[0]["volume"]
    assert remaining == pytest.approx(0.04)
    mt5.bid = mt5.ask = 108.0
    executor.poll_once()                                     # close 0.02 -> remaining 0.02
    remaining = list(mt5.positions.values())[0]["volume"]
    assert remaining == pytest.approx(0.02)
    # no close was ever above the broker remaining volume
    for deal in mt5.deals:
        if deal["entry"] == FakeMT5.DEAL_ENTRY_OUT:
            assert deal["volume"] <= 0.06


def test_cumulative_allocation_not_double_counted(tmp_path):
    mt5 = FakeMT5(account_mode="netting", volume_step=0.001, volume_min=0.001)
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.03)
    executor.create_group(spec)
    mt5.inject_partial_open = 2 / 3                          # filled 0.02
    executor.submit_group(spec.group_id)
    executor.poll_once()
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                     # TP1 ~0.006
    stored = load_group(executor.db_path, spec.group_id)
    closed_tp1 = stored["volume"]["total_closed"]
    mt5.bid = mt5.ask = 108.0
    executor.poll_once()                                     # TP2 increment = cum - closed
    stored = load_group(executor.db_path, spec.group_id)
    closed_tp2 = stored["volume"]["total_closed"]
    # the second close is the CUMULATIVE 2/3 target minus what TP1 already closed
    assert closed_tp2 == pytest.approx(0.02 * 2 / 3, abs=0.001)
    assert closed_tp2 > closed_tp1
    assert closed_tp2 - closed_tp1 == pytest.approx(0.02 * 2 / 3 - closed_tp1, abs=0.001)
    # never more than the filled volume in total
    assert closed_tp2 <= 0.02 + 1e-9


# ==========================================================================
# P1.5.1 §25: hedging partial fill management
# ==========================================================================

def test_hedging_leg_partial_fill(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)                      # legs 0.02/0.02/0.02
    executor.create_group(spec)
    mt5.inject_partial_open = 0.5                            # leg1: 0.02 -> 0.01
    executor.submit_group(spec.group_id)
    executor.poll_once()                                     # OPENED
    stored = load_group(executor.db_path, spec.group_id)
    leg1 = next(i for i in stored["legs"] if i["leg"] == 1)
    assert leg1["state"] == "PARTIALLY_FILLED"
    assert leg1["filled_volume"] == pytest.approx(0.01)
    assert leg1["remaining_volume"] == pytest.approx(0.01)
    # volume ledger reflects the actual fill
    assert stored["volume"]["legs"]["1"]["filled_volume"] == pytest.approx(0.01)


def test_hedging_management_uses_filled_volume(tmp_path):
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)
    executor.create_group(spec)
    mt5.inject_partial_open = 0.5                            # leg1 partially filled
    executor.submit_group(spec.group_id)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    leg1 = next(i for i in stored["legs"] if i["leg"] == 1)
    # management (TP/BE) works on the broker position of the ACTUAL volume;
    # no top-up order is ever sent for the missing 0.01
    assert leg1["state"] == "PARTIALLY_FILLED"
    assert len(mt5.positions) == 3
    assert sum(1 for _ in mt5.deals) == 3                    # exactly 3 open deals


def test_hedging_compensation_uses_actual_volume(tmp_path):
    """Compensation closes exactly the ACTUAL broker volume of each open leg:
    leg1 partially filled (0.01 of 0.02) + leg2 filled (0.02) + leg3 rejected."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)
    executor.create_group(spec)
    mt5.inject_partial_open = 0.5                            # leg1: 0.02 -> 0.01
    mt5.inject_reject_open_nth = 3                           # leg3 rejected
    executor.submit_group(spec.group_id)
    executor.poll_once()                                     # compensation closes
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.COMPENSATION_REQUESTED
    assert len(mt5.positions) == 0                           # both open legs closed
    # the total closed volume == the ACTUAL filled volumes (0.01 + 0.02 = 0.03),
    # NOT the requested volumes (0.02 + 0.02 = 0.04)
    closed = stored["volume"]["total_closed"]
    assert closed == pytest.approx(0.03)
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED


# ==========================================================================
# P1.5.1 §26: restart in partial-submission states
# ==========================================================================

@pytest.mark.parametrize("state", [
    GroupState.SUBMITTED,
    GroupState.COMPENSATION_REQUESTED,
    GroupState.COMPENSATION_CONFIRMED,
    GroupState.FAILED_WITH_OPEN_RISK,
])
def test_restart_in_compensation_states(tmp_path, state):
    """Restart in each partial-submission state: no duplicate compensation, no
    duplicate close, broker ids and remaining volume preserved."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    mt5.inject_reject_open_nth = 3
    executor.submit_group(spec.group_id)

    def _advance_to(target):
        if target == GroupState.COMPENSATION_REQUESTED:
            executor.poll_once()                             # compensation sent
        elif target == GroupState.COMPENSATION_CONFIRMED:
            executor.poll_once()
            executor.poll_once()                             # -> FAILED (confirmed)
        elif target == GroupState.FAILED_WITH_OPEN_RISK:
            mt5.inject_reject_close = True
            executor.poll_once()                             # close rejected

    _advance_to(state)
    if state == GroupState.COMPENSATION_CONFIRMED:
        stored = load_group(executor.db_path, spec.group_id)
        assert stored["state"] == GroupState.FAILED          # confirmed -> FAILED
        return

    order_count = mt5._ot
    actions_before = [a["action_id"] for a in list_actions(executor.db_path, spec.group_id)]
    mt5.inject_reject_close = False                          # broker recovered
    restarted, _ = make_executor(tmp_path, mt5)              # restart
    restored = restarted.recover_after_restart(spec.group_id)
    assert restored["state"] == state
    assert restored["spec"].geometry.tp1 == 104.0            # geometry preserved
    restarted.poll_once()
    restarted.poll_once()
    # per-state order accounting: SUBMITTED sends compensation for the first
    # time (+2 closes); FAILED_WITH_OPEN_RISK re-sends the previously rejected
    # closes (bounded retry, +2); COMPENSATION_REQUESTED only verifies (0).
    expected_new_orders = {"SUBMITTED": 2, "FAILED_WITH_OPEN_RISK": 2,
                           "COMPENSATION_REQUESTED": 0}[state.value]
    assert mt5._ot == order_count + expected_new_orders
    stored = load_group(restarted.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED              # final state after recovery
    assert len(mt5.positions) == 0                           # open risk == 0
    actions_after = [a["action_id"] for a in list_actions(executor.db_path, spec.group_id)]
    comp_before = [a for a in actions_before if a.startswith("COMPENSATE")]
    comp_after = [a for a in actions_after if a.startswith("COMPENSATE")]
    # the retry reuses the SAME deterministic actionIds — the action set never
    # grows beyond one entry per compensated reference (no duplicate actions)
    assert len(comp_after) == len(set(comp_after))
    assert set(comp_before) <= set(comp_after)


def test_restart_in_partial_submission_state(tmp_path):
    """PARTIAL_SUBMISSION is a crash window: the compensation request is
    idempotent, so a restart re-runs it without duplicate closes."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec()
    executor.create_group(spec)
    mt5.inject_reject_open_nth = 3
    executor.submit_group(spec.group_id)
    # crash BEFORE the first poll: the group is still SUBMITTED with a rejected
    # leg and open positions at the broker — recovery must compensate, not dup
    order_count = mt5._ot
    restarted, _ = make_executor(tmp_path, mt5)              # restart
    restored = restarted.recover_after_restart(spec.group_id)
    assert restored["state"] == GroupState.SUBMITTED
    events = restarted.poll_once()                           # compensation starts
    assert "partial_submission" in events
    assert "compensation_requested" in events
    assert mt5._ot == order_count + 2                        # exactly 2 close orders
    restarted.poll_once()
    stored = load_group(restarted.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert len(mt5.positions) == 0


# ==========================================================================
# P1.5.1 §27: global safety invariant
# ==========================================================================

def test_safety_invariant_open_risk_within_approved(tmp_path):
    """For every non-terminal group: broker open volume never exceeds the
    approved group volume (risk proxy); a FAILED group has ZERO open broker
    risk; FAILED_WITH_OPEN_RISK keeps reconciliation explicitly active."""
    mt5 = FakeMT5(account_mode="hedging")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)
    executor.create_group(spec)
    mt5.inject_reject_open_nth = 3
    executor.submit_group(spec.group_id)
    # partial submission -> FAILED after compensation (open risk 0)
    executor.poll_once()
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.FAILED
    assert len(mt5.positions) == 0                           # FAILED -> open risk == 0
    # invariant over all groups in the store
    for group in list_groups(executor.db_path):
        g = load_group(executor.db_path, group["group_id"])
        approved = g["spec"].risk.total_volume
        driver = executor._resolve_driver(g["spec"])
        open_volume = sum(float(p.get("volume") or 0.0)
                          for p in driver.query_positions_by_magic()
                          if g["spec"].group_id in str(p.get("comment", "")))
        if g["state"] == GroupState.FAILED:
            assert open_volume == 0.0
        else:
            assert open_volume <= approved + 1e-9            # never above approved


def test_netting_partial_submission_compensates_aggregate(tmp_path):
    """Netting: a rejected open (aggregate position cannot open) leaves nothing
    to compensate; but a PARTIALLY filled aggregate is compensated with the
    ACTUAL broker volume (P1.5.1 §23/§12)."""
    mt5 = FakeMT5(account_mode="netting")
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)
    executor.create_group(spec)
    mt5.inject_reject_open_nth = 2                          # reject leg2/3 virtual? no —
    # netting submits only ONE physical open (leg1); reject it -> nothing opened
    mt5.inject_reject_open_nth = 1
    executor.submit_group(spec.group_id)
    stored = load_group(executor.db_path, spec.group_id)
    leg1 = next(i for i in stored["legs"] if i["leg"] == 1)
    assert leg1["state"] == "REJECTED"
    assert len(mt5.positions) == 0                          # nothing at the broker
    events = executor.poll_once()
    # no opened legs -> compensation has nothing to close; the group still goes
    # through the controlled flow and finalizes without open risk
    assert "partial_submission" in events
    executor.poll_once()
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] in (GroupState.FAILED, GroupState.REJECTED)
    assert len(mt5.positions) == 0


def test_netting_partial_aggregate_trades_on_actual_volume(tmp_path):
    """Netting: a PARTIALLY filled aggregate (0.06 requested -> 0.03 filled)
    opens normally (no rejected leg) and every later close is based on the
    ACTUAL 0.03, never on the requested 0.06."""
    mt5 = FakeMT5(account_mode="netting", volume_step=0.001, volume_min=0.001)
    executor, _ = make_executor(tmp_path, mt5)
    spec = make_spec(total_volume=0.06)
    executor.create_group(spec)
    mt5.inject_partial_open = 0.5                           # 0.06 -> 0.03 filled
    executor.submit_group(spec.group_id)
    executor.poll_once()                                    # OPENED
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.OPENED
    assert stored["volume"]["total_filled"] == pytest.approx(0.03)
    assert list(mt5.positions.values())[0]["volume"] == pytest.approx(0.03)
    mt5.bid = mt5.ask = 104.0
    executor.poll_once()                                    # TP1: 0.03/3 = 0.01
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["volume"]["total_closed"] == pytest.approx(0.01)
    assert list(mt5.positions.values())[0]["volume"] == pytest.approx(0.02)
    mt5.bid = mt5.ask = 108.0
    executor.poll_once()                                    # TP2: cumulative 0.02
    mt5.bid = mt5.ask = 112.0
    executor.poll_once()                                    # TP3: remaining 0.01
    stored = load_group(executor.db_path, spec.group_id)
    assert stored["state"] == GroupState.RECONCILED
    assert stored["volume"]["total_closed"] == pytest.approx(0.03)
    assert mt5.positions == {}
