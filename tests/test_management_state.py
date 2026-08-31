"""Tests for per-position management-state persistence (execution.mt5_trader).

Regression: _save_management_state used to early-return when active_trades was
empty, leaving long-closed tickets in logs/live_management_state.json. On every
restart the close detector re-reported those tickets as fresh "TRADE CLOSED"
ghost notifications with $0.00 PnL. The file must now be overwritten (purged to
{}) when there are no open positions.
"""
import json

from execution.mt5_trader import MultiAssetMT5Trader, reconcile_position_context


def _trader_instance(path, active_trades):
    trader = MultiAssetMT5Trader.__new__(MultiAssetMT5Trader)
    trader.management_state_path = str(path)
    trader.active_trades = active_trades
    return trader


def test_save_writes_open_positions(tmp_path):
    state_path = tmp_path / "live_management_state.json"
    trader = _trader_instance(state_path, {123: {"symbol": "GOLD", "leg": 2, "tp1": 2000.0}})
    trader._save_management_state()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["123"]["symbol"] == "GOLD"


def test_save_purges_when_no_open_positions(tmp_path):
    """The last position closed but the file still holds stale tickets: saving
    with an empty active_trades must wipe them, not leave them for the next
    restart to re-report as ghost 'TRADE CLOSED' notifications."""
    state_path = tmp_path / "live_management_state.json"
    # Pre-existing stale tickets left on disk by a previous process.
    state_path.write_text(json.dumps({
        "313710949": {"symbol": "BTCUSD", "leg": 2, "tp1": 78089.07},
        "313710953": {"symbol": "BTCUSD", "leg": 3, "tp1": 77839.06},
    }), encoding="utf-8")
    trader = _trader_instance(state_path, {})
    trader._save_management_state()
    assert json.loads(state_path.read_text(encoding="utf-8")) == {}


def test_save_purges_only_closed_tickets(tmp_path):
    """A mix of still-open and just-closed tickets: the open one must be kept,
    the closed one dropped (check_and_move_breakeven pops closed tickets from
    active_trades before saving)."""
    state_path = tmp_path / "live_management_state.json"
    state_path.write_text(json.dumps({
        "313710953": {"symbol": "BTCUSD", "leg": 3, "tp1": 77839.06},
    }), encoding="utf-8")
    trader = _trader_instance(
        state_path,
        {999: {"symbol": "GOLD", "type": "long", "entry_price": 1990.0, "tp1": 2010.0}},
    )
    trader._save_management_state()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert "999" in data
    assert "313710953" not in data


def test_save_leaves_no_tmp_file_behind(tmp_path):
    state_path = tmp_path / "live_management_state.json"
    trader = _trader_instance(state_path, {})
    trader._save_management_state()
    assert json.loads(state_path.read_text(encoding="utf-8")) == {}
    assert not (state_path.parent / (state_path.name + ".tmp")).exists()


def test_reconcile_sweeps_orphaned_positions(tmp_path):
    """live_positions.json holds entries not tracked in active_trades (old
    process left them behind). Reconcile against current open tickets must drop
    the orphaned ones (297282308, 308979629) and keep the still-open ticket."""
    journal = tmp_path / "live_positions.json"
    journal.write_text(json.dumps({
        "999": {"asset_key": "BTCUSD", "bias": "long"},             # still open
        "297282308": {"asset_key": "BTCUSD", "bias": "short"},     # orphan (old position)
        "308979629": {"asset_key": "BTCUSD", "bias": "long"},      # orphan (old position)
    }), encoding="utf-8")
    reconcile_position_context({999}, path=str(journal))
    data = json.loads(journal.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"999"}


def test_reconcile_keeps_empty_open_set_clears_all(tmp_path):
    """No open positions at all -> every journal entry is stale and must go."""
    journal = tmp_path / "live_positions.json"
    journal.write_text(json.dumps({"297282308": {"bias": "short"}}), encoding="utf-8")
    reconcile_position_context(set(), path=str(journal))
    assert json.loads(journal.read_text(encoding="utf-8")) == {}


def test_reconcile_noop_without_changes(tmp_path):
    """Journal already matches open set -> file content untouched (no rewrite)."""
    journal = tmp_path / "live_positions.json"
    journal.write_text(json.dumps({"999": {"bias": "long"}}), encoding="utf-8")
    reconcile_position_context({999}, path=str(journal))
    assert json.loads(journal.read_text(encoding="utf-8")) == {"999": {"bias": "long"}}
    assert not (journal.parent / (journal.name + ".tmp")).exists()


def test_reconcile_missing_file_is_safe(tmp_path):
    journal = tmp_path / "live_positions.json"
    reconcile_position_context({999}, path=str(journal))
    assert not journal.exists()