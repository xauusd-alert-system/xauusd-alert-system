"""ТЗ 7.9 / P2-59: pydantic validation of config.yaml with unknown-key detection.

The loader (``config/loader.py``) validates the parsed YAML against the models
in this module. The goal is to catch typos and structural drift early — e.g.
``max_daily_trades_per_asst`` instead of ``max_daily_trades_per_asset`` —
instead of silently falling back to defaults.

Modes (``validate``):
* ``strict`` — unknown keys / schema violations raise ``ConfigValidationError``;
* ``warn``   (default) — every issue is logged at WARNING with the key name,
  but loading succeeds (backward compatible with existing tests and minimal
  configs);
* ``off``    — no validation.

Mode resolution order: env var ``CONFIG_VALIDATE_MODE`` → top-level
``config_validation.mode`` in the YAML itself → ``warn``.

Scope: the key sections listed in ТЗ 7.9 (general, execution, risk,
mt5_adapter, features.store, provenance, services, monitoring, security) are
strictly modelled (unknown keys inside them are detected). All other sections
(assets, labeling, ensemble, ...) are legacy/lenient: only their presence at
the top level is checked, their internals are not touched.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class ConfigValidationError(ValueError):
    """Raised in strict mode when the config violates the schema."""


@dataclass(frozen=True)
class ConfigIssue:
    """One validation problem: dotted path to the offending key + message."""

    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.path}: {self.message}"


# --------------------------------------------------------------------------
# Section models (ТЗ 7.9). All fields optional with defaults so that minimal
# configs and section absence never fail; extra="forbid" turns a typo into a
# detected issue instead of a silently ignored key.
# --------------------------------------------------------------------------


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- general ---------------------------------------------------------------


class GeneralConfig(_StrictModel):
    db_path: str | None = None


# --- mt5_adapter -----------------------------------------------------------


class Mt5RateLimitConfig(_StrictModel):
    max_calls_per_second: int | None = None


class Mt5CacheConfig(_StrictModel):
    ttl_ms: int | None = None


class MT5AdapterConfig(_StrictModel):
    rate_limit: Mt5RateLimitConfig = Field(default_factory=Mt5RateLimitConfig)
    cache: Mt5CacheConfig = Field(default_factory=Mt5CacheConfig)


# --- security --------------------------------------------------------------


class SecurityApiConfig(_StrictModel):
    require_auth: bool = False


class SecurityConfig(_StrictModel):
    api: SecurityApiConfig = Field(default_factory=SecurityApiConfig)


# --- provenance ------------------------------------------------------------


class ProvenanceStoreConfig(_StrictModel):
    enabled: bool = False
    db_path: str | None = None


class ProvenanceConfig(_StrictModel):
    max_snapshot_age_ms: int | None = None
    store: ProvenanceStoreConfig = Field(default_factory=ProvenanceStoreConfig)


# --- features.store --------------------------------------------------------


class FeatureStoreConfig(_StrictModel):
    enabled: bool = False
    db_path: str | None = None


# --- execution -------------------------------------------------------------


class FxExecutionProbesConfig(_StrictModel):
    enabled: bool = False
    assets: list[str] = Field(default_factory=list)
    volume: float = 1.0
    hold_seconds: int = 2
    min_interval_minutes: int = 120
    max_probes_per_asset_per_day: int = 4
    max_spread_pips: dict[str, float] = Field(default_factory=dict)
    log_path: str | None = None


class TradingBlackoutConfig(_StrictModel):
    enabled: bool = False
    daily_break_utc: list[str] | None = None
    weekend: dict[str, Any] = Field(default_factory=dict)
    flatten_before_minutes: int | None = None
    manual_halt_until_utc: str | None = None


class ExecutionConfig(_StrictModel):
    signal_ttl_ms: dict[str, int] = Field(default_factory=dict)
    enabled_assets: list[str] = Field(default_factory=list)
    require_demo_account: bool = True
    fx_execution_probes: FxExecutionProbesConfig = Field(default_factory=FxExecutionProbesConfig)
    max_open_positions_mode: str = "per_asset"
    max_open_positions_per_asset: int = 2
    max_concurrent_positions_global: int = 6
    max_daily_trades_per_asset: int = 15
    trading_blackout: TradingBlackoutConfig = Field(default_factory=TradingBlackoutConfig)
    volume: float = 1.0
    signal_ranking_metric: str = "confidence"


# --- risk ------------------------------------------------------------------


class CircuitBreakerConfig(_StrictModel):
    exclude_swaps: bool = True


class RiskConfig(_StrictModel):
    enabled: bool = True
    risk_per_trade_pct: float = 0.005
    cluster_risk_cap: float = 0.02
    total_open_risk_cap: float = 0.03
    vol_target: float = 0.1
    leverage_lo: float = 0.5
    leverage_hi: float = 1.25
    same_direction_multiplier: float = 0.35
    # list of [drawdown, scale] pairs, e.g. [[-0.04, 0.75], [-0.08, 0.0]]
    drawdown_throttle: list[list[float]] = Field(default_factory=list)
    min_lot: float = 0.01
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)


# --- services --------------------------------------------------------------


class LedgerBridgeConfig(_StrictModel):
    health_port: int = 8791
    watermark_max_age_min: int = 30


class TelegramBotConfig(_StrictModel):
    health_port: int = 8792


class NewsFeedConfig(_StrictModel):
    health_port: int = 8793
    max_cache_age_hours: int = 6
    refresh_interval_seconds: int = 900


class ServicesConfig(_StrictModel):
    ledger_bridge: LedgerBridgeConfig = Field(default_factory=LedgerBridgeConfig)
    telegram_bot: TelegramBotConfig = Field(default_factory=TelegramBotConfig)
    news_feed: NewsFeedConfig = Field(default_factory=NewsFeedConfig)


# --- monitoring ------------------------------------------------------------


class MonitoringMetricsConfig(_StrictModel):
    jsonl_path: str = "logs/metrics.jsonl"


class MonitoringAlertsConfig(_StrictModel):
    enabled: bool = False
    telegram: bool = True
    feed_stale_after_s: float = 30
    disk_min_free_mb: int = 500
    mt5_disconnect_threshold: int = 5


class MonitoringBackupConfig(_StrictModel):
    dir: str = "backups"
    keep: int = 7


class MonitoringDriftConfig(_StrictModel):
    # P2-40 / TZ 5.3: PSI feature-drift monitoring (overnight stage drift_check).
    enabled: bool = False
    train_csv: Optional[str] = None
    live_csv: Optional[str] = None
    drifted_psi_threshold: float = 0.2


class MonitoringCalibrationConfig(_StrictModel):
    # P2-46 / TZ 5.3: Brier + ECE calibration monitoring
    # (overnight stage calibration_check).
    enabled: bool = False
    input_path: Optional[str] = None
    ece_threshold: float = 0.1


class MonitoringLoggingConfig(_StrictModel):
    format: str = "text"
    max_bytes: int = 10485760
    backup_count: int = 5


class MonitoringConfig(_StrictModel):
    metrics: MonitoringMetricsConfig = Field(default_factory=MonitoringMetricsConfig)
    alerts: MonitoringAlertsConfig = Field(default_factory=MonitoringAlertsConfig)
    backup: MonitoringBackupConfig = Field(default_factory=MonitoringBackupConfig)
    drift: MonitoringDriftConfig = Field(default_factory=MonitoringDriftConfig)
    calibration: MonitoringCalibrationConfig = Field(default_factory=MonitoringCalibrationConfig)
    logging: MonitoringLoggingConfig = Field(default_factory=MonitoringLoggingConfig)


# --------------------------------------------------------------------------
# Schema registry
# --------------------------------------------------------------------------

#: strictly modelled top-level sections (unknown keys inside → issue)
SECTION_MODELS: dict[str, type[BaseModel]] = {
    "general": GeneralConfig,
    "execution": ExecutionConfig,
    "risk": RiskConfig,
    "mt5_adapter": MT5AdapterConfig,
    "provenance": ProvenanceConfig,
    "services": ServicesConfig,
    "monitoring": MonitoringConfig,
    "security": SecurityConfig,
}

#: strictly modelled nested sections ("<section>.<subsection>")
SUBSECTION_MODELS: dict[str, type[BaseModel]] = {
    "features.store": FeatureStoreConfig,
}

#: legacy top-level sections: presence checked, internals lenient
LEGACY_TOP_LEVEL_SECTIONS = {
    "alerts",
    "assets",
    "backtest",
    "book_gate",
    "challenge",
    "correlation_filter",
    "config_validation",
    "deployment",
    "deploy_guard",
    "ensemble",
    "features",
    "labeling",
    "market_data",
    "model",
    "paper_forward",
    "regime",
    "retraining",
    "risk_throttle",
    "sessions",
    "signal_grid",
    "strategy",
    "trade_profiles",
    "validation",
    # Rolling hold-out lock policy (Step 2, 2026-08-30): preregistered
    # cadence/step/tolerance/baseline constants for the conscious lock shift.
    "holdout_roll",
}

KNOWN_TOP_LEVEL_KEYS = set(SECTION_MODELS) | set(SUBSECTION_MODELS) | LEGACY_TOP_LEVEL_SECTIONS


# --------------------------------------------------------------------------
# Validation entry points
# --------------------------------------------------------------------------


def resolve_validation_mode(cfg: dict | None) -> str:
    """Resolve the validation mode: env CONFIG_VALIDATE_MODE → config key → warn."""
    env = os.environ.get("CONFIG_VALIDATE_MODE")
    if env:
        return env.strip().lower()
    section = (cfg or {}).get("config_validation") or {}
    if isinstance(section, dict):
        return str(section.get("mode", "warn")).strip().lower()
    return "warn"


def collect_config_issues(cfg: dict | None) -> list[ConfigIssue]:
    """Collect all unknown-key / schema issues without raising.

    Returns a list (possibly empty). Never mutates ``cfg``.
    """
    issues: list[ConfigIssue] = []
    if not isinstance(cfg, dict):
        return issues

    # 1. unknown top-level keys
    for key in sorted(cfg.keys()):
        if key not in KNOWN_TOP_LEVEL_KEYS:
            issues.append(
                ConfigIssue(path=key, message=f"unknown top-level config key {key!r} (typo or unsupported section?)")
            )

    # 2. strictly modelled top-level sections
    for name, model in SECTION_MODELS.items():
        section = cfg.get(name)
        if section is None:
            continue
        if not isinstance(section, dict):
            issues.append(ConfigIssue(path=name, message=f"section {name!r} must be a mapping"))
            continue
        try:
            model.model_validate(section)
        except Exception as exc:  # pydantic ValidationError
            issues.extend(_pydantic_errors_to_issues(name, exc))

    # 3. strictly modelled nested sections
    for dotted, model in SUBSECTION_MODELS.items():
        parent, _, child = dotted.partition(".")
        section = cfg.get(parent)
        if not isinstance(section, dict):
            continue
        subsection = section.get(child)
        if subsection is None:
            continue
        if not isinstance(subsection, dict):
            issues.append(ConfigIssue(path=dotted, message=f"subsection {dotted!r} must be a mapping"))
            continue
        try:
            model.model_validate(subsection)
        except Exception as exc:
            issues.extend(_pydantic_errors_to_issues(dotted, exc))

    return issues


def _pydantic_errors_to_issues(prefix: str, exc: Exception) -> list[ConfigIssue]:
    """Map a pydantic ValidationError to ConfigIssue entries."""
    issues: list[ConfigIssue] = []
    for err in getattr(exc, "errors", lambda: [])():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        path = f"{prefix}.{loc}" if loc else prefix
        msg = err.get("msg", str(err))
        if err.get("type") == "extra_forbidden":
            msg = f"unknown key {loc!r} in section {prefix!r} (typo?)"
        issues.append(ConfigIssue(path=path, message=msg))
    return issues


def validate_config(cfg: dict | None, mode: str = "warn") -> list[ConfigIssue]:
    """Validate ``cfg`` according to ``mode`` and return the issues found.

    * ``strict`` → raise :class:`ConfigValidationError` if any issue exists;
    * ``warn``   → log each issue at WARNING (with the offending key name);
    * ``off``    → no-op.

    The issues list is returned in every non-``off`` mode for programmatic use.
    """
    mode = (mode or "warn").strip().lower()
    if mode == "off":
        return []
    issues = collect_config_issues(cfg)
    for issue in issues:
        logger.warning("config validation: %s", issue)
    if issues and mode == "strict":
        detail = "; ".join(str(i) for i in issues)
        raise ConfigValidationError(f"config validation failed ({len(issues)} issue(s)): {detail}")
    return issues
