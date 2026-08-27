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
    """Fail-closed default: retraining stays off and execution stays in sync
    with the deliberately enabled models. The owner's 48h demo trial
    (scripts/trial_window.py) unfreezes all 5 assets until the auto-revert
    deadline. Outside a trial, execution.enabled_assets must exactly mirror
    assets.*.enabled=true (the deploy_guard.check_config_sync contract): the
    2026-08-27 owner decision re-enabled the 3 demo assets whose models are
    enabled and dropped the XAGUSD/GBPUSD phantoms. Any drift from either
    contract fails loudly."""
    cfg = load_config()
    assert cfg["retraining"]["enabled"] is False
    assert cfg["retraining"]["schedule"]["enabled"] is False
    if _trial_active():
        assert cfg["execution"]["enabled_assets"] == TRIAL_ASSETS
    else:
        # 2026-08-27 (cb5ce46): partial unfreeze by the owner — the guard is
        # now exact sync with the enabled-model set, not the old deny-all
        enabled_models = sorted(a for a, c in cfg["assets"].items()
                                if c.get("enabled"))
        assert sorted(cfg["execution"]["enabled_assets"]) == enabled_models
    assert configured_execution_assets(cfg) == set(cfg["execution"]["enabled_assets"])