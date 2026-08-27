"""
Config loader utility - shared across all modules.
Ensures a single source of truth: config/config.yaml.
"""
from dotenv import load_dotenv
load_dotenv()
import copy
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


# Global fallback timeframe when neither an explicit override, a per-asset
# `assets.<key>.timeframe`, nor `market_data.timeframe` is present. This is the
# single constant behind resolve_asset_timeframe() — scripts must not each keep
# their own divergent fallback ("M15" vs "M5" drift bug this helper fixes).
DEFAULT_TIMEFRAME = "M5"


def resolve_asset_timeframe(cfg: dict | None, asset_key: str | None,
                            override: str | None = None) -> str:
    """Single source of truth for an asset's effective trading timeframe.

    Priority chain (first non-empty wins):
        1. explicit `override` argument (caller-provided, e.g. a CLI flag);
        2. per-asset `assets.<asset_key>.timeframe`;
        3. global `market_data.timeframe`;
        4. DEFAULT_TIMEFRAME constant.

    Strings pass through unvalidated and un-coerced (an override of "m5"
    stays "m5"); only a *falsy* override ("" / None) is treated as "not
    provided" and falls through to the config chain. A missing cfg, a
    missing `market_data` section, an unknown asset key, or asset_key=None
    are all tolerated and resolve through the remaining chain.
    """
    if override:
        return override
    cfg = cfg or {}
    if asset_key:
        asset_cfg = (cfg.get("assets") or {}).get(asset_key) or {}
        per_asset = asset_cfg.get("timeframe")
        if per_asset:
            return per_asset
    market_data = cfg.get("market_data") or {}
    global_tf = market_data.get("timeframe")
    if global_tf:
        return global_tf
    return DEFAULT_TIMEFRAME


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

# --- books integration (TZ_BOOKS.md) -----------------------------------------

BOOKS_DEFAULTS: dict = {
    "trade_level": 0.60,
    "samples": {
        "asset": "XAUUSD",
        "timeframe": "M5",
        "window": 16,
        "horizon": 12,
        "normalization": "zscore",
        "split": [0.6, 0.2, 0.2],
        "target_mode": "return",
        "multi_horizons": [6, 12],
        "extended_features": False,
    },
    "day_filter": {"enabled": False, "min_trades": 30, "max_win_rate": 0.45},
    "news_guard": {"buffer_before_min": 30, "buffer_after_min": 30,
                   "currencies": ["USD"]},
    "validation": {"min_profit_factor": 1.2, "min_win_rate": 0.55,
                   "min_trades": 30, "forward_min_days": 365,
                   "tick_mode": "real_ticks"},
    "tester_criterion": {"dd_penalty_weight": 1.0, "pf_cap": 10.0,
                         "min_trades": 1},
    "bridge": {"table": "ml_signals", "schema_version": 1,
               "ttl_seconds": 10800},
    "drift": {"psi_warn": 0.10, "psi_alarm": 0.25, "scale_ratio_alarm": 1.5,
              "mean_shift_alarm_sigmas": 1.0},
}


def books_config(cfg: dict | None) -> dict:
    """Effective `books:` section with defaults (see TZ_BOOKS.md).

    The config file only overrides the keys it declares; every omitted key
    falls back to the library default so behaviour never depends on the
    presence of the section. Returns a deep copy - callers may mutate.
    """
    import copy
    section = (cfg or {}).get("books") or {}
    if not isinstance(section, dict):
        raise ValueError("config 'books:' section must be a mapping")
    out = copy.deepcopy(BOOKS_DEFAULTS)
    for key, value in section.items():
        if key not in out:
            raise KeyError(f"unknown books config key {key!r}")
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key].update(value)
        else:
            out[key] = value
    return out
