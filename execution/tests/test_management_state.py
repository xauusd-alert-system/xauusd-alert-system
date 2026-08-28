"""Tests for the audit W10 fix: per-position management state (TP targets,
partial-hit flags) is persisted and restored across a process restart, so a
restart keeps managing still-open positions instead of dropping them to the
broker TP/SL."""
import json

from execution.mt5_trader import MultiAssetMT5Trader


def _trader(state_path):
    t = object.__new__(MultiAssetMT5Trader)
    t.management_state_path = state_path
    t._load_management_state()
    return t


def test_save_and_restore_management_state(tmp_path):
    state = str(tmp_path / "mgmt.json")
    t = _trader(state)
    t.active_trades = {
        1001: {
            "symbol": "GOLD", "type": "long", "entry_price": 2000.0,
            "original_volume": 0.10, "tp1": 2005.0, "tp2": 2010.0, "tp3": 2015.0,
            "tp1_hit": True, "tp2_hit": False,
        }
    }
    t._save_management_state()
    assert json.load(open(state))["1001"]["tp1"] == 2005.0

    # A "restarted" trader restores the state. Keys come back as INT tickets so
    # they match the runtime pos.ticket type (a str key would make the close
    # detector treat a still-open position as closed after a restart).
    t2 = _trader(state)
    assert 1001 in t2.active_trades
    assert "1001" not in t2.active_trades
    assert t2.active_trades[1001]["tp1"] == 2005.0
    assert t2.active_trades[1001]["tp1_hit"] is True


def test_load_ignores_entries_without_tp_targets(tmp_path):
    """Entries with tp1=None (e.g. never recorded) are not restored, because the
    manager cannot act on them."""
    state = str(tmp_path / "mgmt.json")
    json.dump({"9999": {"symbol": "GOLD", "tp1": None, "tp1_hit": False}},
              open(state, "w"))
    t = _trader(state)
    assert 9999 not in t.active_trades


def test_save_does_not_clobber_when_empty(tmp_path):
    state = str(tmp_path / "mgmt.json")
    t = _trader(state)
    t.active_trades = {}
    # Nothing open -> must not write an empty map (no clobber of existing).
    t._save_management_state()
    assert not __import__("os").path.exists(state)
