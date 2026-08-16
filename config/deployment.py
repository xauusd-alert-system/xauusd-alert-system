"""Formal deployment modes replacing ambiguous DRY_RUN comments."""
from enum import Enum


class DeploymentMode(str, Enum):
    SIMULATION = "simulation"
    RESEARCH = "research"
    PAPER = "paper"
    HUMAN_CONFIRMED = "human_confirmed"
    DEMO_SYSTEMATIC = "demo_systematic"
    LIVE_SYSTEMATIC = "live_systematic"


def deployment_mode(cfg: dict) -> DeploymentMode:
    raw = (cfg or {}).get("deployment", {}).get("mode", "research")
    return DeploymentMode(str(raw))


def order_routing_allowed(cfg: dict, *, confirmed_by: str | None = None) -> tuple[bool, str]:
    mode = deployment_mode(cfg)
    if mode in {DeploymentMode.SIMULATION, DeploymentMode.RESEARCH, DeploymentMode.PAPER}:
        return False, f"deployment mode {mode.value} prohibits broker orders"
    if mode == DeploymentMode.HUMAN_CONFIRMED and not confirmed_by:
        return False, "human_confirmed mode requires an auditable confirmer"
    if mode == DeploymentMode.LIVE_SYSTEMATIC and not cfg.get("deployment", {}).get("live_approval_id"):
        return False, "live_systematic requires deployment.live_approval_id"
    return True, "allowed"
