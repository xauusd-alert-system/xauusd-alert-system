from datetime import datetime, timezone

import scripts.trial_window as tw
from config.loader import load_config
from execution.mt5_trader import configured_execution_assets

TRIAL_ASSETS = ["BTCUSD", "XAUUSD", "XAGUSD", "EURUSD", "GBPUSD"]
# Baseline (non-trial) trading set. The config no longer freezes execution to
# an empty list: XAGUSD/GBPUSD were removed 2026-08-27 because their
# assets.*.enabled=false, but BTCUSD/XAUUSD/EURUSD stay explicitly enabled.
# See config/config.yaml execution.enabled_assets comment block.
BASELINE_ASSETS = ["BTCUSD", "XAUUSD", "EURUSD"]


def _trial_active() -> bool:
    try:
        state = tw.load_state()
    except Exception:
        return False
    if not state or state.get("reverted", False):
        return False
    try:
        ends = datetime.fromisoformat(state["ends_at_utc"])
    except (KeyError, TypeError, ValueError):
        return False
    return ends > datetime.now(timezone.utc)


def test_geometry_revalidation_safety_freeze_is_fail_closed():
    """Fail-closed default: retraining stays off and execution stays pinned to
    the documented baseline asset set (XAGUSD/GBPUSD excluded because their
    assets.*.enabled=false). The owner's 48h demo trial (scripts/trial_window.py)
    unfreezes all 5 assets until the auto-revert deadline. The test asserts the
    config matches whichever contract is currently active, so a config that
    drifts from the trial window (or a trial that fails to revert) fails loudly."""
    cfg = load_config()
    assert cfg["retraining"]["enabled"] is False
    assert cfg["retraining"]["schedule"]["enabled"] is False
    if _trial_active():
        assert cfg["execution"]["enabled_assets"] == TRIAL_ASSETS
    else:
        assert cfg["execution"]["enabled_assets"] == BASELINE_ASSETS
    assert configured_execution_assets(cfg) == set(cfg["execution"]["enabled_assets"])
