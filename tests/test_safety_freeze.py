from datetime import datetime, timezone

from config.loader import load_config
from execution.mt5_trader import configured_execution_assets
import scripts.trial_window as tw

TRIAL_ASSETS = ["BTCUSD", "XAUUSD", "XAGUSD", "EURUSD", "GBPUSD"]


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
    """Fail-closed default: retraining stays off and no asset may trade while
    the geometry-revalidation freeze is in force. The owner's 48h demo trial
    (scripts/trial_window.py) is the ONLY documented exception: it explicitly
    unfreezes all 5 assets until the auto-revert deadline. The test asserts
    the config matches whichever contract is currently active, so a config
    that drifts from the trial window (or a trial that fails to revert) fails
    loudly."""
    cfg = load_config()
    assert cfg["retraining"]["enabled"] is False
    assert cfg["retraining"]["schedule"]["enabled"] is False
    if _trial_active():
        assert cfg["execution"]["enabled_assets"] == TRIAL_ASSETS
    else:
        assert cfg["execution"]["enabled_assets"] == []
    assert configured_execution_assets(cfg) == set(cfg["execution"]["enabled_assets"])