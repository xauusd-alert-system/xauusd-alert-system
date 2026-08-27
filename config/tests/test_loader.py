"""Regression tests for config.loader helpers that scripts depend on.

Context: commit 3aea2ac shipped tests for resolve_asset_timeframe without the
implementation itself in config/loader.py, breaking test collection. These
tests pin the restored behavior at the import boundary (DEFAULT_TIMEFRAME +
resolve_asset_timeframe must be importable together with the existing API).
"""
from config.loader import DEFAULT_TIMEFRAME, load_config, resolve_asset_timeframe


def test_default_timeframe_constant():
    assert DEFAULT_TIMEFRAME == "M5"


def test_priority_chain_override_beats_asset_and_global():
    cfg = {"market_data": {"timeframe": "M5"}, "assets": {"XAUUSD": {"timeframe": "M15"}}}
    assert resolve_asset_timeframe(cfg, "XAUUSD", override="H4") == "H4"


def test_none_cfg_tolerated():
    assert resolve_asset_timeframe(None, "XAUUSD") == DEFAULT_TIMEFRAME


def test_real_config_resolves_without_error():
    # The production config must resolve for the flagship asset.
    cfg = load_config()
    tf = resolve_asset_timeframe(cfg, "XAUUSD")
    assert isinstance(tf, str) and tf
