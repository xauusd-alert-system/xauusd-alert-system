"""
Config loader utility - shared across all modules.
Ensures a single source of truth: config/config.yaml.
"""
from dotenv import load_dotenv
load_dotenv()
import os
import yaml

_CONFIG_CACHE = None


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


def get_env(key: str, default=None, required: bool = False):
    """
    Fetch a secret/config value from environment variables.
    """
    val = os.environ.get(key, default)
    if required and val is None:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return val


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