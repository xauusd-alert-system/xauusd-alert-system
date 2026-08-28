"""ТЗ 7.9 / P2-59: tests for config/schema.py + loader integration.

Covers:
* the real production config passes validation;
* a typo in a key is detected in warn mode (WARNING logged) and raises in
  strict mode;
* a missing section falls back to model defaults;
* a minimal config still loads (backward compatibility of existing tests).
"""
from __future__ import annotations

import logging

import pytest
import yaml

from config import loader
from config.schema import (
    ConfigValidationError,
    collect_config_issues,
    resolve_validation_mode,
    validate_config,
)


@pytest.fixture
def real_config() -> dict:
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CONFIG_VALIDATE_MODE", raising=False)


def test_valid_current_config_passes(real_config):
    """The production config/config.yaml must produce zero issues."""
    issues = collect_config_issues(real_config)
    assert issues == []
    # strict mode must also accept it
    assert validate_config(real_config, mode="strict") == []


def test_typo_detected_in_warn_mode(real_config, caplog):
    """A typo like max_daily_trades_per_asst is logged as WARNING in warn
    mode but does not raise."""
    cfg = dict(real_config)
    typo_section = dict(cfg["execution"])
    typo_section["max_daily_trades_per_asst"] = 15  # typo: per_asst
    cfg["execution"] = typo_section

    with caplog.at_level(logging.WARNING, logger="config.schema"):
        issues = validate_config(cfg, mode="warn")

    assert issues, "typo must be detected"
    assert any("max_daily_trades_per_asst" in str(i) for i in issues)
    assert any(
        "max_daily_trades_per_asst" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )


def test_typo_raises_in_strict_mode(real_config):
    """The same typo raises ConfigValidationError in strict mode."""
    cfg = dict(real_config)
    typo_section = dict(cfg["execution"])
    typo_section["max_daily_trades_per_asst"] = 15
    cfg["execution"] = typo_section

    with pytest.raises(ConfigValidationError) as exc:
        validate_config(cfg, mode="strict")
    assert "max_daily_trades_per_asst" in str(exc.value)


def test_unknown_top_level_key_detected(real_config):
    cfg = dict(real_config)
    cfg["executiion"] = {"volume": 1}  # typo at top level

    issues = collect_config_issues(cfg)
    assert any("executiion" in str(i) for i in issues)

    with pytest.raises(ConfigValidationError):
        validate_config(cfg, mode="strict")


def test_missing_section_uses_defaults():
    """An absent section is fine: models carry defaults, minimal configs load."""
    issues = collect_config_issues({})
    assert issues == []
    # explicitly empty sections also validate against defaults
    assert validate_config({"general": None, "risk": {}, "execution": {}},
                           mode="strict") == []


def test_minimal_config_still_loads():
    """Backward compatibility: a minimal/legacy config (only legacy sections)
    loads without errors even in strict mode."""
    minimal = {
        "labeling": {"tp1_atr_multiplier": 1.0},
        "assets": {"XAUUSD": {"timeframe": "M15"}},
    }
    assert validate_config(minimal, mode="strict") == []


def test_loader_warn_mode_loads_typo_config(real_config, tmp_path, caplog, monkeypatch):
    """Loader integration: warn mode (default) still loads a config with a
    typo, while logging WARNING; strict mode raises through load_config."""
    cfg = dict(real_config)
    typo_section = dict(cfg["execution"])
    typo_section["max_daily_trades_per_asst"] = 15
    cfg["execution"] = typo_section
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    monkeypatch.setattr(loader, "_CONFIG_CACHE", None)

    # warn (default)
    monkeypatch.delenv("CONFIG_VALIDATE_MODE", raising=False)
    with caplog.at_level(logging.WARNING, logger="config.schema"):
        loaded = loader.load_config(str(path))
    assert loaded["execution"]["max_daily_trades_per_asst"] == 15
    assert any("max_daily_trades_per_asst" in r.message for r in caplog.records)

    # strict — but load_config caches; reset cache and set env override
    monkeypatch.setattr(loader, "_CONFIG_CACHE", None)
    monkeypatch.setenv("CONFIG_VALIDATE_MODE", "strict")
    with pytest.raises(ConfigValidationError):
        loader.load_config(str(path))

    # restore cache state so other tests are unaffected
    monkeypatch.setattr(loader, "_CONFIG_CACHE", None)


def test_validation_mode_resolution(monkeypatch):
    assert resolve_validation_mode({}) == "warn"
    assert resolve_validation_mode({"config_validation": {"mode": "strict"}}) == "strict"
    monkeypatch.setenv("CONFIG_VALIDATE_MODE", "off")
    assert resolve_validation_mode({"config_validation": {"mode": "strict"}}) == "off"
    monkeypatch.delenv("CONFIG_VALIDATE_MODE", raising=False)
    assert resolve_validation_mode(None) == "warn"


def test_validate_off_mode_noop(real_config):
    cfg = dict(real_config)
    cfg["executiion"] = {"volume": 1}
    assert validate_config(cfg, mode="off") == []
