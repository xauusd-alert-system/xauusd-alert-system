"""Tests for config.loader.resolve_asset_timeframe — the single source of
truth for an asset's effective trading timeframe.

The priority chain is: explicit override -> assets.<key>.timeframe ->
market_data.timeframe -> DEFAULT_TIMEFRAME. This helper exists because ~30
scripts historically re-implemented the chain with divergent hardcoded
fallbacks (e.g. "M15"), which made diagnostics run on a different tier than
production for assets without an explicit per-asset timeframe.
"""

import pytest

from config.loader import DEFAULT_TIMEFRAME, resolve_asset_timeframe


def _cfg(**overrides) -> dict:
    cfg = {
        "market_data": {"timeframe": "M5"},
        "assets": {
            "XAUUSD": {"timeframe": "M15"},
            "BTCUSD": {"enabled": True},          # no per-asset TF -> global
            "EURUSD": {"timeframe": "H1"},
        },
    }
    cfg.update(overrides)
    return cfg


def test_per_asset_override_wins():
    cfg = _cfg()
    assert resolve_asset_timeframe(cfg, "XAUUSD") == "M15"
    assert resolve_asset_timeframe(cfg, "EURUSD") == "H1"


def test_global_default_when_no_per_asset():
    cfg = _cfg()
    assert resolve_asset_timeframe(cfg, "BTCUSD") == "M5"


def test_explicit_override_beats_everything():
    cfg = _cfg()
    assert resolve_asset_timeframe(cfg, "XAUUSD", "H4") == "H4"
    assert resolve_asset_timeframe(cfg, "BTCUSD", "M1") == "M1"


def test_unknown_asset_falls_through_to_global():
    cfg = _cfg()
    assert resolve_asset_timeframe(cfg, "NOPE") == "M5"


def test_none_asset_returns_global_default():
    cfg = _cfg()
    assert resolve_asset_timeframe(cfg, None) == "M5"


def test_missing_market_data_falls_back_to_constant():
    cfg = _cfg()
    del cfg["market_data"]
    assert resolve_asset_timeframe(cfg, "XAUUSD") == "M15"   # per-asset still wins
    assert resolve_asset_timeframe(cfg, "BTCUSD") == DEFAULT_TIMEFRAME
    assert DEFAULT_TIMEFRAME == "M5"


def test_none_cfg_is_tolerated():
    assert resolve_asset_timeframe(None, "XAUUSD") == DEFAULT_TIMEFRAME
    assert resolve_asset_timeframe({}, "XAUUSD") == DEFAULT_TIMEFRAME


def test_asset_key_without_cfg_entry_tolerated():
    cfg = _cfg()
    # Simulates the per-asset key existing in the loop but not in config.
    assert resolve_asset_timeframe(cfg, "SOLUSD") == "M5"


@pytest.mark.parametrize("tf_in,expected", [
    ("m5", "m5"),        # strings pass through, not coerced/validated
    ("", ""),            # falsy empty string is treated as "no override"
    ("  ", "  "),        # whitespace is truthy and passes through
])
def test_override_passthrough(tf_in, expected):
    cfg = _cfg()
    if tf_in == "":
        # empty override behaves like "not provided" -> per-asset/global chain
        assert resolve_asset_timeframe(cfg, "XAUUSD", "") == "M15"
    else:
        assert resolve_asset_timeframe(cfg, "XAUUSD", tf_in) == expected
