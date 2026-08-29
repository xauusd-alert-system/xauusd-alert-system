"""Unit tests for the pure helpers of MultiAssetMT5Trader (Phase 5, step 2c).

Scope: the helper methods that can be exercised without a running trader loop,
without loading the production config and without an MT5 terminal:

    _normalize_stops, _get_min_dist, _be_min_dist, _normalize_tp_level,
    _leg_volumes, _scaleout_volume, _get_dynamic_min_confidence,
    _update_corr_matrix, _are_correlated, _has_correlated_position,
    _validate_contract_sizes, _intent_created_fact, _request_result_fact

The trader is built with ``object.__new__`` and only the attributes the helper
under test actually reads are assigned — ``__init__`` loads config, starts the
book feed and builds ML pipelines, none of which these helpers touch.

MT5 access goes through mt5_adapter.testing.MockMT5Module. NOTE: mt5_trader
resolves ``mt5 = get_mt5_module()`` at IMPORT time, so patching
``mt5_adapter.lazy.get_mt5_module`` leaves the module handle untouched — the
``mt5`` attribute on the trader module itself is what must be swapped (same
approach as test_fx_execution_probe.py).

Deliberately out of scope (steps 2d/2e): run_loop(), execute_signal(),
check_and_move_breakeven(), main(). The blackout helpers (_dow_utc,
_blackout_status, _in_daily_break) live in test_blackout.py, which already owns
the blackout fixtures.
"""

from __future__ import annotations

import logging
import time
import types

import pandas as pd
import pytest
from pydantic import ValidationError

from contracts.execution_contracts import build_signal_intent, execution_event_id
from execution import mt5_trader as trader_mod
from execution.mt5_trader import MultiAssetMT5Trader
from mt5_adapter.testing import TRADE_RETCODE_DONE, TRADE_RETCODE_REJECT, MockMT5Module, _OrderResultTuple

TRADER_LOG = "multi_asset_trader"


# ---------------------------------------------------------------------------
# Doubles and fixtures
# ---------------------------------------------------------------------------


class _SymInfo:
    """Minimal symbol_info double carrying only the fields the helpers read."""

    def __init__(self, point=0.01, digits=2, stops=0, freeze=0, step=0.01, min_lot=0.01, contract=100.0):
        self.point = point
        self.digits = digits
        self.trade_stops_level = stops
        self.trade_freeze_level = freeze
        self.volume_step = step
        self.volume_min = min_lot
        self.trade_contract_size = contract


class _Tick:
    def __init__(self, bid, ask):
        self.bid = bid
        self.ask = ask


def _trader(**attrs):
    t = object.__new__(MultiAssetMT5Trader)
    for name, value in attrs.items():
        setattr(t, name, value)
    return t


@pytest.fixture()
def mock_mt5(monkeypatch):
    """Replace the trader module's import-time mt5 handle with a MockMT5Module."""
    mock = MockMT5Module()  # trade_mode defaults to ACCOUNT_TRADE_MODE_DEMO
    mock.initialize()  # account_info()/positions_get() return None until then
    monkeypatch.setattr(trader_mod, "mt5", mock)
    return mock


# GOLD: point 0.01, stops 10 pts, freeze 5 pts, spread 30 pts
# -> min_dist = max(10, 5, 30 + 30) = 60 pts = 0.60
def _gold(mock, bid: float = 2400.00, ask: float = 2400.30, **info_overrides):
    info = {"point": 0.01, "digits": 2, "trade_stops_level": 10, "trade_freeze_level": 5}
    info.update(info_overrides)
    mock.set_symbol_info("XAUUSD", **info)
    mock.set_tick("XAUUSD", bid=bid, ask=ask)


# ---------------------------------------------------------------------------
# _normalize_stops
# ---------------------------------------------------------------------------


def test_normalize_stops_widens_too_tight_long_stops(mock_mt5):
    _gold(mock_mt5)
    t = _trader()
    sl, tp = t._normalize_stops("XAUUSD", "long", 2400.30, 2399.50, 2400.50)
    assert sl == pytest.approx(2399.40)  # min(raw_sl, bid - min_dist)
    assert tp == pytest.approx(2400.90)  # max(raw_tp, ask + min_dist)


def test_normalize_stops_keeps_long_stops_that_are_already_wide(mock_mt5):
    _gold(mock_mt5)
    t = _trader()
    sl, tp = t._normalize_stops("XAUUSD", "long", 2400.30, 2390.00, 2410.00)
    assert sl == pytest.approx(2390.00)
    assert tp == pytest.approx(2410.00)


def test_normalize_stops_widens_too_tight_short_stops(mock_mt5):
    _gold(mock_mt5)
    t = _trader()
    sl, tp = t._normalize_stops("XAUUSD", "short", 2400.00, 2400.50, 2399.50)
    assert sl == pytest.approx(2400.90)  # max(raw_sl, ask + min_dist)
    assert tp == pytest.approx(2399.40)  # min(raw_tp, bid - min_dist)


def test_normalize_stops_keeps_short_stops_that_are_already_wide(mock_mt5):
    _gold(mock_mt5)
    t = _trader()
    sl, tp = t._normalize_stops("XAUUSD", "short", 2400.00, 2410.00, 2390.00)
    assert sl == pytest.approx(2410.00)
    assert tp == pytest.approx(2390.00)


def test_normalize_stops_snaps_to_the_symbol_point_grid(mock_mt5):
    _gold(mock_mt5)
    t = _trader()
    sl, tp = t._normalize_stops("XAUUSD", "long", 2400.30, 2390.004, 2410.006)
    assert sl == pytest.approx(2390.00)
    assert tp == pytest.approx(2410.01)


def test_normalize_stops_uses_the_stops_level_when_it_dominates(mock_mt5):
    """stops_level 200 pts (2.00) beats freeze 50 pts (0.50) and the spread
    buffer (2 pts + 30 = 0.32)."""
    _gold(mock_mt5, bid=2400.00, ask=2400.02, trade_stops_level=200, trade_freeze_level=50)
    t = _trader()
    sl, tp = t._normalize_stops("XAUUSD", "long", 2400.02, 2399.00, 2401.00)
    assert sl == pytest.approx(2398.00)
    assert tp == pytest.approx(2402.02)


def test_normalize_stops_uses_the_freeze_level_when_it_dominates(mock_mt5):
    """freeze_level 150 pts (1.50) beats stops 20 pts (0.20) and the spread
    buffer (0.32)."""
    _gold(mock_mt5, bid=2400.00, ask=2400.02, trade_stops_level=20, trade_freeze_level=150)
    t = _trader()
    sl, tp = t._normalize_stops("XAUUSD", "long", 2400.02, 2399.00, 2401.00)
    assert sl == pytest.approx(2398.50)
    assert tp == pytest.approx(2401.52)


@pytest.mark.parametrize("missing", ["info", "tick"])
def test_normalize_stops_raises_without_symbol_data(mock_mt5, missing):
    _gold(mock_mt5)
    if missing == "info":
        mock_mt5.symbol_infos.pop("XAUUSD")
    else:
        mock_mt5.ticks.pop("XAUUSD")
    t = _trader()
    with pytest.raises(RuntimeError, match="symbol_info failed"):
        t._normalize_stops("XAUUSD", "long", 2400.30, 2390.00, 2410.00)


# ---------------------------------------------------------------------------
# _get_min_dist / _be_min_dist
# ---------------------------------------------------------------------------


def test_get_min_dist_uses_the_spread_buffer_when_it_dominates():
    t = _trader()
    got = t._get_min_dist("XAUUSD", _Tick(2400.00, 2400.30), _SymInfo(point=0.01, stops=10, freeze=5))
    assert got == pytest.approx(0.60)  # max(0.10, 0.05, 0.30 + 30 * 0.01)


def test_get_min_dist_uses_the_stops_level_when_it_dominates():
    t = _trader()
    got = t._get_min_dist("XAUUSD", _Tick(2400.00, 2400.02), _SymInfo(point=0.01, stops=100, freeze=5))
    assert got == pytest.approx(1.00)


def test_get_min_dist_uses_the_freeze_level_when_it_dominates():
    t = _trader()
    got = t._get_min_dist("XAUUSD", _Tick(2400.00, 2400.02), _SymInfo(point=0.01, stops=10, freeze=80))
    assert got == pytest.approx(0.80)


def test_be_min_dist_ignores_the_spread_buffer():
    """The breakeven minimum uses only the broker's own stops/freeze levels
    (audit 2026-08-18: the +30 pts buffer made a BE move look blocked)."""
    t = _trader()
    assert t._be_min_dist(_SymInfo(point=0.01, stops=20, freeze=0)) == pytest.approx(0.20)
    assert t._be_min_dist(_SymInfo(point=0.01, stops=0, freeze=30)) == pytest.approx(0.30)
    assert t._be_min_dist(_SymInfo(point=0.1, stops=5, freeze=0)) == pytest.approx(0.5)


def test_be_min_dist_falls_back_to_zero_when_fields_are_missing():
    t = _trader()
    assert t._be_min_dist(types.SimpleNamespace()) == 0.0
    # Levels without a point size still yield no distance.
    assert t._be_min_dist(types.SimpleNamespace(trade_stops_level=10)) == 0.0


# ---------------------------------------------------------------------------
# _normalize_tp_level
# ---------------------------------------------------------------------------


def test_normalize_tp_level_pushes_a_too_close_long_tp_out(mock_mt5):
    _gold(mock_mt5)
    t = _trader()
    assert t._normalize_tp_level("XAUUSD", "long", 2400.50) == pytest.approx(2400.90)
    assert t._normalize_tp_level("XAUUSD", "long", 2410.00) == pytest.approx(2410.00)


def test_normalize_tp_level_pulls_a_too_close_short_tp_in(mock_mt5):
    _gold(mock_mt5)
    t = _trader()
    assert t._normalize_tp_level("XAUUSD", "short", 2399.50) == pytest.approx(2399.40)
    assert t._normalize_tp_level("XAUUSD", "short", 2390.00) == pytest.approx(2390.00)


def test_normalize_tp_level_snaps_to_the_symbol_point_grid(mock_mt5):
    _gold(mock_mt5)
    t = _trader()
    assert t._normalize_tp_level("XAUUSD", "long", 2410.006) == pytest.approx(2410.01)


def test_normalize_tp_level_raises_without_symbol_data(mock_mt5):
    mock_mt5.set_tick("XAUUSD", bid=2400.00, ask=2400.30)  # no symbol_info
    t = _trader()
    with pytest.raises(RuntimeError, match="symbol_info failed"):
        t._normalize_tp_level("XAUUSD", "long", 2410.00)


# ---------------------------------------------------------------------------
# _leg_volumes
# ---------------------------------------------------------------------------


def test_leg_volumes_split_evenly():
    t = _trader(volume=0.09)
    assert t._leg_volumes(_SymInfo()) == pytest.approx([0.03, 0.03, 0.03])


def test_leg_volumes_leg3_absorbs_the_lot_step_remainder():
    """0.30 / 3 floors to 0.09 per leg, so leg 3 takes the leftover 0.12 and
    the legs always sum back to the configured volume."""
    t = _trader(volume=0.30)
    vols = t._leg_volumes(_SymInfo())
    assert vols == pytest.approx([0.09, 0.09, 0.12])
    assert sum(vols) == pytest.approx(t.volume)


def test_leg_volumes_logs_legs_below_the_broker_minimum(caplog):
    """A broker min lot above the lot step makes every leg unfillable: the
    volumes are still returned (the caller skips them) but each one is logged."""
    t = _trader(volume=0.06)
    with caplog.at_level(logging.WARNING, logger=TRADER_LOG):
        vols = t._leg_volumes(_SymInfo(step=0.01, min_lot=0.05))
    assert vols == pytest.approx([0.02, 0.02, 0.02])
    assert "Leg 1 volume" in caplog.text
    assert "min fillable 0.05" in caplog.text


def test_leg_volumes_does_not_log_legs_that_round_to_zero(caplog):
    """A base lot below one step leaves legs 1-2 at 0.0 (nothing to warn about)
    and puts the whole volume on leg 3."""
    t = _trader(volume=0.02)
    with caplog.at_level(logging.WARNING, logger=TRADER_LOG):
        vols = t._leg_volumes(_SymInfo())
    assert vols == pytest.approx([0.0, 0.0, 0.02])
    assert "min fillable" not in caplog.text


# ---------------------------------------------------------------------------
# _scaleout_volume (complements execution/tests/test_scaleout_volume.py)
# ---------------------------------------------------------------------------


def test_scaleout_volume_skips_a_tranche_below_the_broker_minimum(caplog):
    """The tranche is a valid multiple of the lot step but still under
    volume_min, so it must be skipped as well — not only the round-to-zero case
    covered in test_scaleout_volume.py."""
    t = _trader()
    info = _SymInfo(step=0.01, min_lot=0.05)
    with caplog.at_level(logging.WARNING, logger=TRADER_LOG):
        assert t._scaleout_volume("XAUUSD", info, 0.06, 0.5) == 0.0
    assert "min fillable 0.05" in caplog.text


def test_scaleout_volume_falls_back_to_the_lot_step_without_volume_min():
    """Brokers that report no volume_min: the step becomes the minimum instead
    of raising or dividing by zero."""
    t = _trader()
    info = types.SimpleNamespace(volume_step=0.01, volume_min=None)
    assert t._scaleout_volume("XAUUSD", info, 0.10, 0.5) == pytest.approx(0.05)
    assert t._scaleout_volume("XAUUSD", info, 0.01, 0.5) == 0.0


# ---------------------------------------------------------------------------
# _get_dynamic_min_confidence
# ---------------------------------------------------------------------------


def _conf_trader(cfg: dict, streak: dict):
    return _trader(cfg=cfg, streak_losses=streak)


def test_min_confidence_prefers_the_per_asset_override():
    cfg = {
        "assets": {"XAUUSD": {"ensemble": {"min_confidence_to_alert": 0.65}}},
        "ensemble": {"min_confidence_to_alert": 0.70},
    }
    assert _conf_trader(cfg, {})._get_dynamic_min_confidence("XAUUSD") == pytest.approx(0.65)


def test_min_confidence_falls_back_to_the_global_ensemble():
    cfg = {"assets": {"XAUUSD": {"ensemble": {}}}, "ensemble": {"min_confidence_to_alert": 0.70}}
    assert _conf_trader(cfg, {})._get_dynamic_min_confidence("XAUUSD") == pytest.approx(0.70)


def test_min_confidence_falls_back_to_0_60():
    cfg = {"assets": {"XAUUSD": {"ensemble": {}}}}
    assert _conf_trader(cfg, {})._get_dynamic_min_confidence("XAUUSD") == pytest.approx(0.60)


@pytest.mark.parametrize(
    ("streak", "extra"),
    [(0, 0.0), (1, 0.0), (2, 0.03), (3, 0.06), (4, 0.09), (5, 0.10), (9, 0.10)],
)
def test_min_confidence_escalates_with_the_loss_streak(streak, extra):
    """+0.03 per loss from the 2nd one on, capped at +0.10."""
    cfg = {"assets": {"XAUUSD": {"ensemble": {"min_confidence_to_alert": 0.60}}}}
    t = _conf_trader(cfg, {"XAUUSD": streak})
    assert t._get_dynamic_min_confidence("XAUUSD") == pytest.approx(0.60 + extra)


# ---------------------------------------------------------------------------
# Correlation filter
# ---------------------------------------------------------------------------


def _wave(n: int, phase: int = 0) -> list[float]:
    """Deterministic, non-constant return series (integer sawtooth)."""
    return [float(((i + phase) % 11) - 5) for i in range(n)]


def _returns(values: list[float], name: str, start: str = "2026-01-01") -> pd.Series:
    index = pd.date_range(start, periods=len(values), freq="h", tz="UTC")
    return pd.Series(values, index=index, name=name)


def _corr_trader(cfg=None, matrix=None, threshold=0.80):
    cfg = cfg if cfg is not None else {}
    return _trader(
        cfg=cfg,
        corr_filter_cfg=(cfg.get("correlation_filter") or {}),
        corr_threshold=threshold,
        corr_history_bars=500,
        corr_update_interval=60,
        corr_matrix=matrix if matrix is not None else {},
        corr_last_update=0,
        magic_number=777111,
    )


TWO_ASSETS = {
    "assets": {
        "XAUUSD": {"enabled": True, "mt5_symbol": "GOLD"},
        "XAGUSD": {"enabled": True, "mt5_symbol": "SILVER"},
        "EURUSD": {"enabled": False, "mt5_symbol": "EURUSD"},
    }
}


def test_update_corr_matrix_skips_inside_the_update_interval():
    t = _corr_trader(TWO_ASSETS)
    t.corr_last_update = time.time()
    t._update_corr_matrix()
    assert t.corr_matrix == {}


def test_update_corr_matrix_builds_the_matrix_from_enabled_assets():
    t = _corr_trader(TWO_ASSETS)
    fetched = []

    def fake_fetch(asset_key, symbol, count):
        fetched.append((asset_key, symbol, count))
        return _returns(_wave(120), asset_key)

    t._fetch_close_series = fake_fetch
    t._update_corr_matrix()
    # The disabled asset is never fetched; the two enabled ones are.
    assert [f[0] for f in fetched] == ["XAUUSD", "XAGUSD"]
    assert fetched[0][2] == 500  # corr_history_bars
    assert t.corr_matrix["XAUUSD"]["XAGUSD"] == pytest.approx(1.0)
    assert t.corr_matrix["XAGUSD"]["XAUUSD"] == pytest.approx(1.0)
    assert t.corr_last_update > 0


def test_update_corr_matrix_needs_at_least_two_assets():
    t = _corr_trader({"assets": {"XAUUSD": {"enabled": True, "mt5_symbol": "GOLD"}}})
    t._fetch_close_series = lambda asset_key, symbol, count: _returns(_wave(120), asset_key)
    t._update_corr_matrix()
    assert t.corr_matrix == {}


def test_update_corr_matrix_ignores_series_that_are_too_short():
    """A series of <= 50 bars is dropped; with one usable series there is no
    matrix to build."""
    t = _corr_trader(TWO_ASSETS)

    def fake_fetch(asset_key, symbol, count):
        return _returns(_wave(120 if asset_key == "XAUUSD" else 40), asset_key)

    t._fetch_close_series = fake_fetch
    t._update_corr_matrix()
    assert t.corr_matrix == {}


def test_update_corr_matrix_needs_enough_overlapping_rows():
    """Both series are long enough on their own, but the aligned (dropna) table
    has fewer than 50 rows -> no matrix."""
    t = _corr_trader(TWO_ASSETS)

    def fake_fetch(asset_key, symbol, count):
        start = "2026-01-01" if asset_key == "XAUUSD" else "2026-01-20"
        return _returns(_wave(120), asset_key, start=start)

    t._fetch_close_series = fake_fetch
    t._update_corr_matrix()
    assert t.corr_matrix == {}


def test_are_correlated_is_false_without_a_matrix():
    assert _corr_trader()._are_correlated("XAUUSD", "XAGUSD") is False


@pytest.mark.parametrize(
    ("corr", "expected"),
    [(0.95, True), (0.80, True), (0.79, False), (-0.95, True), (-0.80, True), (-0.79, False), (0.0, False)],
)
def test_are_correlated_compares_the_absolute_value(corr, expected):
    """The threshold is inclusive (>=) and sign-agnostic: a strong inverse
    correlation blocks a same-direction entry just as a positive one does."""
    t = _corr_trader(matrix={"XAUUSD": {"XAGUSD": corr}})
    assert t._are_correlated("XAUUSD", "XAGUSD") is expected


def test_are_correlated_is_false_for_unknown_pairs():
    t = _corr_trader(matrix={"XAUUSD": {"XAGUSD": 0.95}})
    assert t._are_correlated("XAUUSD", "EURUSD") is False
    assert t._are_correlated("EURUSD", "XAUUSD") is False


def _corr_cfg():
    return dict(TWO_ASSETS, correlation_filter={"enabled": True})


def test_has_correlated_position_is_off_when_the_filter_is_disabled():
    t = _corr_trader({"correlation_filter": {"enabled": False}, "assets": {}})
    assert t._has_correlated_position("XAUUSD", "long") is False


def test_has_correlated_position_without_open_positions(mock_mt5):
    t = _corr_trader(_corr_cfg())
    t.corr_last_update = time.time()  # keep _update_corr_matrix a no-op
    assert t._has_correlated_position("XAUUSD", "long") is False


def test_has_correlated_position_blocks_the_same_direction(mock_mt5):
    t = _corr_trader(_corr_cfg(), matrix={"XAUUSD": {"XAGUSD": 0.95}})
    t.corr_last_update = time.time()
    mock_mt5.add_position("SILVER", type=0)  # long
    assert t._has_correlated_position("XAUUSD", "long") is True
    assert t._has_correlated_position("XAUUSD", "short") is False


def test_has_correlated_position_ignores_the_same_asset(mock_mt5):
    t = _corr_trader(_corr_cfg(), matrix={"XAUUSD": {"XAGUSD": 0.95}})
    t.corr_last_update = time.time()
    mock_mt5.add_position("GOLD", type=0)
    assert t._has_correlated_position("XAUUSD", "long") is False


def test_has_correlated_position_ignores_symbols_of_no_configured_asset(mock_mt5):
    t = _corr_trader(_corr_cfg(), matrix={"XAUUSD": {"XAGUSD": 0.95}})
    t.corr_last_update = time.time()
    mock_mt5.add_position("USDCAD", type=0)
    assert t._has_correlated_position("XAUUSD", "long") is False


def test_has_correlated_position_ignores_uncorrelated_assets(mock_mt5):
    t = _corr_trader(_corr_cfg(), matrix={"XAUUSD": {"XAGUSD": 0.10}})
    t.corr_last_update = time.time()
    mock_mt5.add_position("SILVER", type=0)
    assert t._has_correlated_position("XAUUSD", "long") is False


def test_has_correlated_position_ignores_positions_of_another_magic(mock_mt5):
    t = _corr_trader(_corr_cfg(), matrix={"XAUUSD": {"XAGUSD": 0.95}})
    t.corr_last_update = time.time()
    mock_mt5.add_position("SILVER", type=0, magic=999)
    assert t._has_correlated_position("XAUUSD", "long") is False


def test_has_correlated_position_refreshes_the_matrix(mock_mt5):
    t = _corr_trader(_corr_cfg(), matrix={"XAUUSD": {"XAGUSD": 0.95}})
    calls = []
    t._update_corr_matrix = lambda: calls.append(1)
    mock_mt5.add_position("SILVER", type=0)
    assert t._has_correlated_position("XAUUSD", "long") is True
    assert calls == [1]


# ---------------------------------------------------------------------------
# _validate_contract_sizes
# ---------------------------------------------------------------------------


def test_validate_contract_sizes_warns_on_point_value_mismatch(mock_mt5, caplog):
    mock_mt5.set_symbol_info("GOLD", trade_contract_size=100.0)
    cfg = {"assets": {"XAUUSD": {"mt5_symbol": "GOLD", "point_value_lot": 10.0}}}
    with caplog.at_level(logging.WARNING, logger=TRADER_LOG):
        _trader(cfg=cfg, volume=0.30)._validate_contract_sizes()
    assert "point_value_lot=10.0" in caplog.text
    assert "trade_contract_size=100.0" in caplog.text


def test_validate_contract_sizes_is_quiet_when_the_contract_matches(mock_mt5, caplog):
    mock_mt5.set_symbol_info("GOLD", trade_contract_size=100.0)
    cfg = {"assets": {"XAUUSD": {"mt5_symbol": "GOLD", "point_value_lot": 100.0}}}
    with caplog.at_level(logging.WARNING, logger=TRADER_LOG):
        _trader(cfg=cfg, volume=0.30)._validate_contract_sizes()
    assert caplog.text == ""


def test_validate_contract_sizes_warns_on_an_unfillable_scaleout(mock_mt5, caplog):
    mock_mt5.set_symbol_info("GOLD", trade_contract_size=100.0, volume_step=0.01, volume_min=0.05)
    cfg = {"assets": {"XAUUSD": {"mt5_symbol": "GOLD", "point_value_lot": 100.0}}}
    with caplog.at_level(logging.WARNING, logger=TRADER_LOG):
        _trader(cfg=cfg, volume=0.06)._validate_contract_sizes()
    assert "50% scale-out of volume 0.06" in caplog.text
    assert "min fillable 0.05" in caplog.text


def test_validate_contract_sizes_skips_assets_without_symbol_or_terminal_info(mock_mt5, caplog):
    """No mt5_symbol (research-only asset) or no symbol_info in the terminal ->
    skipped, never a warning."""
    cfg = {"assets": {"XAUUSD": {"point_value_lot": 1.0}, "XAGUSD": {"mt5_symbol": "SILVER"}}}
    with caplog.at_level(logging.WARNING, logger=TRADER_LOG):
        _trader(cfg=cfg, volume=0.30)._validate_contract_sizes()
    assert caplog.text == ""


def test_validate_contract_sizes_ignores_a_zero_volume_step(mock_mt5, caplog):
    """A terminal that reports no contract size / lot step cannot be compared
    against the config, so it must stay silent rather than warn on zeros."""
    mock_mt5.set_symbol_info("GOLD", trade_contract_size=0.0, volume_step=0.0, volume_min=0.0)
    cfg = {"assets": {"XAUUSD": {"mt5_symbol": "GOLD", "point_value_lot": 100.0}}}
    with caplog.at_level(logging.WARNING, logger=TRADER_LOG):
        _trader(cfg=cfg, volume=0.06)._validate_contract_sizes()
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# Execution facts (Wave-0 contracts)
# ---------------------------------------------------------------------------


def _intent():
    return build_signal_intent(
        asset_key="XAUUSD",
        broker_symbol="GOLD",
        side="long",
        requested_volume=0.09,
        entry_price=2400.0,
        sl_price=2390.0,
        tp_price=2410.0,
        mode="demo_systematic",
        magic_number=777111,
        signal_id="sig-1",
        created_at_utc_ms=1_700_000_000_000,
    )


def test_intent_created_fact_carries_the_intent_identity(mock_mt5):
    intent = _intent()
    event = _trader()._intent_created_fact(intent)
    assert event.event_type == "intent_created"
    assert event.intent_id == intent.intent_id
    assert event.source == "mt5_python_sender"
    assert event.account_mode == "demo"
    assert event.broker_symbol == "GOLD"
    assert event.asset_key == "XAUUSD"
    assert event.magic_number == 777111
    assert event.volume_requested == pytest.approx(0.09)
    assert event.precision == "request"
    assert event.payload == {"signal_id": "sig-1", "mode": "demo_systematic"}
    assert event.event_id == execution_event_id("mt5_python_sender", "demo:12345", "intent", intent.intent_id)


def test_intent_created_fact_breaks_on_an_unknown_account_mode(mock_mt5):
    """DEFECT (pinned, not endorsed) — reported during phase 5 / step 2c.

    _account_fingerprint() falls back to the mode string "unknown" whenever
    mt5.account_info() is unavailable (terminal disconnected) or reports a
    trade_mode outside ACCOUNT_TRADE_MODE_{DEMO,CONTEST,REAL}. ExecutionEvent
    constrains account_mode to Literal["demo", "real", "contest"], so building
    the fact raises instead of recording it.

    Blast radius today: _intent_created_fact is called inside a try/except at
    mt5_trader.py:1646-1650, so the intent fact is silently dropped. The
    _request_result_fact call at mt5_trader.py:1714-1723 is NOT guarded — it
    runs right after order_send, so a raise there skips the retcode handling,
    the position registration and the Telegram alert for every remaining leg.

    execution/mt5_trader.py is owner-WIP in this phase and must not be
    modified, so the fix is deferred. When it lands, replace this pin with an
    assertion that the fact is built with a coerced/valid account_mode.
    """
    mock_mt5.account = mock_mt5.account._replace(trade_mode=42)
    with pytest.raises(ValidationError, match="account_mode"):
        _trader()._intent_created_fact(_intent())


def _result(retcode=TRADE_RETCODE_DONE, order=555, deal=666, volume=0.09, price=2401.5, comment=""):
    return _OrderResultTuple(
        retcode=retcode,
        deal=deal,
        order=order,
        volume=volume,
        price=price,
        comment=comment,
        request_id=1,
        retcode_external=0,
    )


def _request():
    return {"action": 1, "magic": 777111, "price": 2400.5, "volume": 0.09, "comment": "3leg-1"}


def _request_fact(result, requested_at_ms=1_700_000_000_000, intent_id="intent-1"):
    return _trader()._request_result_fact(
        intent_id=intent_id,
        asset_key="XAUUSD",
        broker_symbol="GOLD",
        request=_request(),
        result=result,
        requested_at_ms=requested_at_ms,
    )


def test_request_result_fact_reports_a_fill(mock_mt5):
    event = _request_fact(_result())
    assert event.event_type == "request_result"
    assert event.intent_id == "intent-1"
    assert event.order_ticket == 555
    assert event.deal_ticket == 666
    assert event.retcode == TRADE_RETCODE_DONE
    assert event.fill_price == pytest.approx(2401.5)
    assert event.filled_volume == pytest.approx(0.09)
    assert event.requested_price == pytest.approx(2400.5)
    assert event.volume_requested == pytest.approx(0.09)
    assert event.magic_number == 777111
    assert event.reason is None
    assert event.latency_ms >= 0
    assert event.payload == {"action": "1", "order_comment": "3leg-1"}
    assert event.event_id == execution_event_id("mt5_python_sender", "demo:12345", "request", "555:666")


def test_request_result_fact_reports_a_rejection(mock_mt5):
    event = _request_fact(
        _result(retcode=TRADE_RETCODE_REJECT, order=0, deal=0, volume=0.0, price=0.0, comment="no money")
    )
    assert event.order_ticket is None
    assert event.deal_ticket is None
    assert event.fill_price is None  # not a fill -> no price reported
    assert event.filled_volume is None
    assert event.retcode == TRADE_RETCODE_REJECT
    assert event.reason == "no money"
    assert event.precision == "request"
    # No order/deal ticket -> the id falls back to timestamp + retcode.
    expected_tx = f"t:1700000000000:{TRADE_RETCODE_REJECT}"
    assert event.event_id == execution_event_id("mt5_python_sender", "demo:12345", "request", expected_tx)


def test_request_result_fact_drops_non_positive_fill_data(mock_mt5):
    """Some MT5 result types report 0 for price/volume even on a DONE retcode;
    that must never be turned into a fake 0.0 fill."""
    event = _request_fact(_result(price=0.0, volume=0.0))
    assert event.retcode == TRADE_RETCODE_DONE
    assert event.fill_price is None
    assert event.filled_volume is None
    assert event.reason is None
