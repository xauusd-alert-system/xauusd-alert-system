from config.loader import load_config
from execution.mt5_trader import configured_execution_assets


def test_geometry_revalidation_safety_freeze_is_fail_closed():
    cfg = load_config()
    assert cfg["retraining"]["enabled"] is False
    assert cfg["retraining"]["schedule"]["enabled"] is False
    assert cfg["execution"]["enabled_assets"] == []
    assert configured_execution_assets(cfg) == set()
