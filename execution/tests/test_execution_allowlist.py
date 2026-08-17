from execution.mt5_trader import configured_execution_assets


def test_explicit_empty_execution_allowlist_denies_all():
    cfg = {
        "assets": {"XAUUSD": {"enabled": True}, "BTCUSD": {"enabled": True}},
        "execution": {"enabled_assets": []},
    }
    assert configured_execution_assets(cfg) == set()


def test_missing_allowlist_preserves_legacy_enabled_asset_fallback():
    cfg = {
        "assets": {"XAUUSD": {"enabled": True}, "XAGUSD": {"enabled": False}},
        "execution": {},
    }
    assert configured_execution_assets(cfg) == {"XAUUSD"}
