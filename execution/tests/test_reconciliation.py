"""Unit tests for execution/reconciliation.py (ТЗ §27/§28)."""

from __future__ import annotations

from execution.reconciliation import (
    classify_broker_close,
    inspect_group,
    latest_out_deal,
)
from execution.trade_group import GroupState, TradeGroupSpec


def _spec(side="long") -> TradeGroupSpec:
    if side == "long":
        entry, tp1, tp2, tp3, sl = 100.0, 104.0, 108.0, 112.0, 90.0
    else:
        entry, tp1, tp2, tp3, sl = 100.0, 96.0, 92.0, 88.0, 110.0
    return TradeGroupSpec(
        group_id="TG-REC-1",
        signal_id="SGL-REC-1",
        intent_id="INT-REC-1",
        asset_key="XAUUSD",
        broker_symbol="GOLD",
        mode="demo",
        side=side,
        entry={"low": 99.0, "high": 101.0, "reference": entry},
        geometry={"version": "v1", "unit": "price", "step_price": 4.0, "tp1": tp1, "tp2": tp2, "tp3": tp3, "sl": sl},
        targets=[
            {"leg": 1, "price": tp1, "allocation": 1 / 3},
            {"leg": 2, "price": tp2, "allocation": 1 / 3},
            {"leg": 3, "price": tp3, "allocation": 1 / 3},
        ],
        break_even={
            "trigger": "tp1_filled",
            "raw_price_policy": "actual_fill",
            "protected_price_policy": "actual_fill_plus_cost_buffer",
            "apply_to": [2, 3],
        },
        risk={"currency": "USD", "max_cash": 50.0, "max_pct": 0.5, "estimated_loss_at_sl": 30.0, "total_volume": 0.03},
        profile_id="v1",
        model_version="v3",
        model_hash="m" * 64,
        config_hash="c" * 64,
        strategy_version="s3",
        expires_at_utc_ms=1_900_000_000_000,
        created_at_utc_ms=1_700_000_000_000,
    )


def test_classify_broker_close_long():
    spec = _spec("long")
    assert classify_broker_close(spec, {"price": 104.0}) == "tp1"
    assert classify_broker_close(spec, {"price": 108.0}) == "tp2"
    assert classify_broker_close(spec, {"price": 112.0}) == "tp3"
    assert classify_broker_close(spec, {"price": 90.0}) == "stop"
    # near-miss with tolerance
    assert classify_broker_close(spec, {"price": 104.1}, tolerance=0.2) == "tp1"
    # nearest-level fallback for an off-level price
    assert classify_broker_close(spec, {"price": 106.5}) == "tp2"


def test_classify_broker_close_short():
    spec = _spec("short")
    assert classify_broker_close(spec, {"price": 96.0}) == "tp1"
    assert classify_broker_close(spec, {"price": 92.0}) == "tp2"
    assert classify_broker_close(spec, {"price": 88.0}) == "tp3"
    assert classify_broker_close(spec, {"price": 110.0}) == "stop"


class _FakeDriver:
    """Minimal driver double exposing the inspection surface."""

    def __init__(self, positions=None, deals=None):
        self._positions = positions or []
        self._deals = deals or {}

    def query_positions_by_magic(self):
        return list(self._positions)

    def query_deals(self, ticket):
        return list(self._deals.get(ticket, []))


def test_inspect_group_detects_broker_closed_leg_via_history():
    """ТЗ §28: local group OPENED, broker position GONE -> OUT deal in history
    must be the confirmation evidence (not the absence of the position)."""
    spec = _spec()
    group = {
        "group_id": spec.group_id,
        "spec": spec,
        "state": GroupState.OPENED,
        "submitted": True,
        "legs": [
            {"leg": 1, "volume": 0.01, "state": "OPEN", "broker": {"position_id": 100001}},
            {"leg": 2, "volume": 0.01, "state": "OPEN", "broker": {"position_id": 100002}},
            {"leg": 3, "volume": 0.01, "state": "OPEN", "broker": {"position_id": 100003}},
        ],
    }
    # leg1 position is gone; its OUT deal (broker TP at 104) lives in history
    driver = _FakeDriver(
        positions=[
            {"ticket": 100002, "volume": 0.01, "comment": f"TG:{spec.group_id}|L:{spec.group_id}-L2"},
            {"ticket": 100003, "volume": 0.01, "comment": f"TG:{spec.group_id}|L:{spec.group_id}-L3"},
        ],
        deals={
            100001: [{"ticket": 10, "position": 100001, "entry": 1, "price": 104.0, "time": 100, "volume": 0.01}],
            100002: [],
            100003: [],
        },
    )
    inspection = inspect_group(driver, group)
    assert inspection.broker_closed is True
    out = latest_out_deal(inspection)
    assert out["price"] == 104.0
    assert classify_broker_close(spec, out) == "tp1"


def test_inspect_group_volume_mismatch_netting():
    """Netting: one aggregate position covering all virtual legs; a shortfall
    must be reported as an exact volume mismatch."""
    spec = _spec()
    group = {
        "group_id": spec.group_id,
        "spec": spec,
        "state": GroupState.OPENED,
        "submitted": True,
        "legs": [
            {"leg": 1, "volume": 0.02, "broker": {"position_id": 100001}},
            {"leg": 2, "volume": 0.02, "broker": {"position_id": 100001}},
            {"leg": 3, "volume": 0.02, "broker": {"position_id": 100001}},
        ],
    }
    driver = _FakeDriver(
        positions=[{"ticket": 100001, "volume": 0.04, "comment": f"TG:{spec.group_id}|L:{spec.group_id}-L1"}],
        deals={100001: []},
    )
    inspection = inspect_group(driver, group)
    assert len(inspection.volume_mismatch) == 1
    mismatch = inspection.volume_mismatch[0]
    assert mismatch["expected"] == 0.06
    assert mismatch["actual"] == 0.04
    assert mismatch["legs"] == [1, 2, 3]
