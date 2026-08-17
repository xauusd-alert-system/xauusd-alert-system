"""Tests for data/trade_group_store.py — durable TradeGroupSpec persistence."""
from __future__ import annotations

import pytest

from data.trade_group_store import (
    init_trade_group_store,
    is_submitted,
    list_groups,
    load_group,
    save_group,
    try_mark_submitted,
    update_group_state,
)
from execution.trade_group import GroupState, TradeGroupSpec


def _spec(group_id: str = "TG-TEST-1", mode: str = "paper") -> TradeGroupSpec:
    return TradeGroupSpec(
        group_id=group_id,
        signal_id="SGL-1", intent_id="INT-1",
        asset_key="XAUUSD", broker_symbol="GOLD", mode=mode, side="long",
        entry={"low": 4159.10, "high": 4159.50, "reference": 4159.30},
        geometry={"version": "v1", "unit": "price", "step_price": 4.30,
                  "tp1": 4163.60, "tp2": 4167.70, "tp3": 4171.20, "sl": 4140.30},
        targets=[{"leg": 1, "price": 4163.60, "allocation": 0.333333},
                 {"leg": 2, "price": 4167.70, "allocation": 0.333333},
                 {"leg": 3, "price": 4171.20, "allocation": 0.333334}],
        break_even={"trigger": "tp1_filled",
                    "raw_price_policy": "actual_fill",
                    "protected_price_policy": "actual_fill_plus_cost_buffer",
                    "apply_to": [2, 3]},
        risk={"currency": "USD", "max_cash": 25.0, "max_pct": 0.5,
              "estimated_loss_at_sl": 24.0, "total_volume": 0.03},
        profile_id="xau_m15_intraday_v1",
        model_version="v3", model_hash="m", config_hash="c", strategy_version="s",
        expires_at_utc_ms=1_800_000_000_000, created_at_utc_ms=1_700_000_000_000,
    )


def test_save_load_roundtrip(tmp_path):
    db = str(tmp_path / "groups.sqlite")
    save_group(db, _spec(), state=GroupState.VALIDATED)
    loaded = load_group(db, "TG-TEST-1")
    assert loaded is not None
    assert loaded["spec"].group_id == "TG-TEST-1"
    assert loaded["state"] == GroupState.VALIDATED
    assert loaded["spec"].geometry.tp1 == 4163.60
    assert loaded["submitted"] is False
    assert loaded["legs"] == []


def test_update_state_preserves_geometry(tmp_path):
    db = str(tmp_path / "groups2.sqlite")
    spec = _spec()
    save_group(db, spec, state=GroupState.VALIDATED)
    update_group_state(db, spec.group_id, GroupState.SUBMITTED,
                       legs=[{"leg": 1, "state": "SUBMITTED"}],
                       broker_ids={"TG-TEST-1-L1": {"order_id": "O1"}})
    loaded = load_group(db, spec.group_id)
    assert loaded["state"] == GroupState.SUBMITTED
    assert loaded["legs"][0]["leg"] == 1
    assert loaded["broker_ids"]["TG-TEST-1-L1"]["order_id"] == "O1"
    assert loaded["spec"].geometry.tp1 == 4163.60  # immutable geometry


def test_submitted_guard_is_sticky(tmp_path):
    """Restart safety (ТЗ §25/§28.8): only the FIRST submit wins."""
    db = str(tmp_path / "groups3.sqlite")
    spec = _spec()
    save_group(db, spec, state=GroupState.VALIDATED)
    assert is_submitted(db, spec.group_id) is False
    assert try_mark_submitted(db, spec.group_id) is True
    assert is_submitted(db, spec.group_id) is True
    # second submit attempt (e.g. after restart) must fail
    assert try_mark_submitted(db, spec.group_id) is False
    # unknown group -> False, never raises
    assert try_mark_submitted(db, "TG-UNKNOWN") is False


def test_list_groups_filter(tmp_path):
    db = str(tmp_path / "groups4.sqlite")
    save_group(db, _spec("TG-A"), state=GroupState.VALIDATED)
    save_group(db, _spec("TG-B"), state=GroupState.TP1_FILLED)
    assert {g["spec"].group_id for g in list_groups(db)} == {"TG-A", "TG-B"}
    only_tp1 = list_groups(db, state=GroupState.TP1_FILLED)
    assert [g["spec"].group_id for g in only_tp1] == ["TG-B"]
    assert load_group(db, "TG-MISSING") is None
