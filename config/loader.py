"""
Config loader utility - shared across all modules.
Ensures a single source of truth: config/config.yaml.
"""
from dotenv import load_dotenv
load_dotenv()
import copy
import os
import sys
import yaml

_CONFIG_CACHE = None

# ---------------------------------------------------------------------------
# Deployment profiles (docs/MIGRATION_PLAN.md, Stage B).
#   forex_legacy / crypto_legacy — historical auto-trading behaviour; this is
#       also the DEFAULT when neither --profile nor PROFILE is given, so all
#       existing entry points keep working unchanged.
#   us_stocks_challenge / replay — SIGNAL-ONLY: executable entry points refuse
#       to start (usstocks.guards) and the only executor is DisabledExecutor.
# ---------------------------------------------------------------------------
KNOWN_PROFILES = ("us_stocks_challenge", "replay", "forex_legacy", "crypto_legacy")
DEFAULT_PROFILE = "forex_legacy"
SIGNAL_ONLY_PROFILES = ("us_stocks_challenge", "replay")


def get_profile() -> str:
    """Resolve the active profile.

    Priority: ``--profile <name>`` CLI flag > ``PROFILE`` env var >
    ``DEFAULT_PROFILE``. Unknown names fail fast instead of silently falling
    back to an auto-trading-capable profile.
    """
    raw = None
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--profile" and i + 1 < len(argv):
            raw = argv[i + 1]
            break
        if arg.startswith("--profile="):
            raw = arg.split("=", 1)[1]
            break
    if raw is None:
        raw = os.environ.get("PROFILE")
    profile = (raw or DEFAULT_PROFILE).strip()
    if profile not in KNOWN_PROFILES:
        raise ValueError(
            f"Unknown PROFILE {profile!r}; expected one of: "
            f"{', '.join(KNOWN_PROFILES)}"
        )
    return profile


def is_signal_only(profile: str = None) -> bool:
    """True when the (explicitly passed or active) profile forbids execution."""
    return (profile or get_profile()) in SIGNAL_ONLY_PROFILES


def load_config(path: str = None) -> dict:
    """
    Load and cache the master YAML config with explicit UTF-8 encoding.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.yaml")

    with open(path, "r", encoding="utf-8") as f:  # <-- Добавлен encoding="utf-8"
        _CONFIG_CACHE = yaml.safe_load(f)

    return _CONFIG_CACHE


def effective_asset_config(cfg: dict, asset_key: str) -> dict:
    """Return a deep-copied config with all supported per-asset sections merged.

    Training, validation and live inference must resolve the same policy.  Keeping
    this operation in the config layer avoids the historical failure where the
    research runner merged ``assets.<KEY>.labeling`` but production retraining did
    not.  Unknown asset keys are rejected: silently falling back to global costs
    or target geometry would create a model with an untraceable contract.
    """
    assets = (cfg or {}).get("assets", {}) or {}
    if asset_key not in assets:
        raise KeyError(f"Unknown asset_key {asset_key!r}; no assets.{asset_key} config")

    out = copy.deepcopy(cfg)
    asset_cfg = assets[asset_key] or {}
    for section in ("labeling", "model", "ensemble", "signal_grid"):
        override = asset_cfg.get(section)
        if override is None:
            continue
        if not isinstance(override, dict):
            raise ValueError(f"assets.{asset_key}.{section} must be a mapping")
        merged = copy.deepcopy(out.get(section, {}) or {})
        merged.update(copy.deepcopy(override))
        out[section] = merged
    return out


def get_env(key: str, default=None, required: bool = False):
    """
    Fetch a secret/config value from environment variables.
    """
    val = os.environ.get(key, default)
    if required and val is None:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val


def resolve_signal_step(atr_value: float, grid: dict) -> float:
    """Resolve the causal grid step from signal-bar ATR and configured clamps."""
    atr_value = float(atr_value)
    if not atr_value > 0:
        raise ValueError(f"ATR must be positive, got {atr_value!r}")
    fixed = grid.get("step_points")
    step = float(fixed) if fixed is not None else atr_value
    lower = grid.get("step_min_points")
    upper = grid.get("step_max_points")
    if lower is not None:
        step = max(step, float(lower))
    if upper is not None:
        step = min(step, float(upper))
    if not step > 0:
        raise ValueError(f"resolved signal step must be positive, got {step!r}")
    return step


def get_signal_grid(cfg: dict, asset_cfg: dict = None, regime: str = None) -> dict:
    """
    Effective signal-grid config (the equal-step TP/SL grid sent to Telegram).

    Source of truth is the `signal_grid:` section (top-level, optionally
    overridden per-asset via assets.<key>.signal_grid). For backward
    compatibility with minimal/test configs that only carry `labeling:` keys,
    the legacy tp*/stop_atr_multiplier values are used as the base and then
    overridden by `signal_grid`. Normalized keys:

      tp1_mult / tp2_mult / tp3_mult / stop_mult  — grid ratios relative to
          the resolved step (spec: 1 / 2 / 3 / 3)
      breakeven_trigger_atr  — fraction of the TP1 distance at which the stop
          is moved to entry BEFORE TP1 (early breakeven). 1.0 = legacy (BE only
          when TP1 hits); < 1.0 (e.g. 0.5) protects mean-reverting assets (FX)
          from the 3x-step loss tail by converting would-be losers into
          scratches (default 1.0 keeps the legacy behaviour).
      step_points       — optional fixed step in price points (overrides the
          dynamic ATR step when set)
      step_min_points   — lower clamp for the step (None = no clamp)
      step_max_points   — upper clamp for the step (None = no clamp)
    """
    lab = cfg.get("labeling", {})
    grid = {
        "tp1_mult": float(lab.get("tp1_atr_multiplier", 1.0)),
        "tp2_mult": float(lab.get("tp2_atr_multiplier", 2.0)),
        "tp3_mult": float(lab.get("tp3_atr_multiplier", 3.0)),
        "stop_mult": float(lab.get("stop_atr_multiplier", 3.0)),
        "breakeven_trigger_atr": float(lab.get("breakeven_trigger_atr", 1.0)),
        "step_points": lab.get("step_points"),
        "step_min_points": lab.get("step_min_points"),
        "step_max_points": lab.get("step_max_points"),
    }
    grid.update({k: v for k, v in cfg.get("signal_grid", {}).items() if v is not None})
    if asset_cfg:
        grid.update(
            {k: v for k, v in asset_cfg.get("signal_grid", {}).items() if v is not None}
        )
    # trailing_atr_mult (for v4b "trailing-runner"): None = legacy (no trailing)
    # When set (e.g. 2.0), after TP2 the remaining 20% position is trailed at trailing*ATR
    # from the most recent high (long) / low (short).
    grid["trailing_atr_mult"] = None
    if asset_cfg and "trailing_atr_mult" in asset_cfg.get("signal_grid", {}):
        grid["trailing_atr_mult"] = asset_cfg.get("signal_grid", {}).get("trailing_atr_mult")
    elif "trailing_atr_mult" in cfg.get("signal_grid", {}):
        grid["trailing_atr_mult"] = cfg.get("signal_grid", {}).get("trailing_atr_mult")

    # Per-regime exit policy (quant audit 2026-08-07, Claude plan action 4):
    # the audit's law — in trend regimes manage WIDE (later/no early BE, far
    # targets, optional trailing), in range/compression manage FAST (early BE,
    # tight stop). Overrides live under signal_grid.regime_overrides.<regime>
    # (top-level or per-asset), e.g.
    #   signal_grid:
    #     regime_overrides:
    #       trend_up:   {stop_mult: 4.0, breakeven_trigger_atr: 1.0, tp2_mult: 2.5,
    #                    tp3_mult: 4.0, scaleout: {tp1_ratio: 0.3, tp2_ratio: 0.3}}
    #       range:      {stop_mult: 2.0, breakeven_trigger_atr: 0.5,
    #                    scaleout: {tp1_ratio: 0.6, tp2_ratio: 0.4}}
    # When `regime` is given and a matching override exists, its keys are
    # layered on top of the effective grid (non-None values only), so the base
    # equal-step spec stays the default everywhere else.
    if regime and regime is not None:
        overrides = {}
        top_ro = cfg.get("signal_grid", {}).get("regime_overrides")
        asset_ro = asset_cfg.get("signal_grid", {}).get("regime_overrides") if asset_cfg else None
        if isinstance(top_ro, dict):
            overrides.update(top_ro)
        if isinstance(asset_ro, dict):
            overrides.update(asset_ro)
        ro = overrides.get(str(regime))
        if isinstance(ro, dict):
            for k, v in ro.items():
                if v is not None and k != "regime_overrides":
                    grid[k] = v
    return grid