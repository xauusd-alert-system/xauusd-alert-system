# -*- coding: utf-8 -*-
"""Stage B contract of config/us_stocks_challenge.yaml: the profile must be
born signal-only. Loosening any of these keys requires an explicit, reviewed
config change — never a code default."""
import os

import yaml

_PROFILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "us_stocks_challenge.yaml",
)


def _load() -> dict:
    with open(_PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_execution_mode_is_disabled():
    cfg = _load()
    assert cfg["execution"]["mode"] == "disabled"


def test_signal_only_flag_is_true():
    assert _load()["signal_only"] is True


def test_model_is_advisory_only_and_disabled():
    model = _load()["model"]
    assert model["enabled"] is False
    assert model["role"] == "advisory_only"
    assert model["min_quality_score"] is None


def test_risk_limits_match_tz_section2():
    risk = _load()["risk"]
    assert risk["risk_per_trade_usd"] == 10.0
    assert risk["personal_daily_stop_usd"] == -20.0
    assert risk["max_trades_per_day"] == 2
    assert risk["max_consecutive_losses"] == 2
    assert risk["daily_profit_lock_usd"] == 20.0
    assert risk["no_new_entries_minutes_before_close"] == 25


def test_challenge_limits_match_official_conditions():
    ch = _load()["challenge"]
    assert ch["account_size_usd"] == 1000.0
    assert ch["official_daily_loss_limit_usd"] == 50.0
    assert ch["official_total_loss_limit_usd"] == 100.0
    assert ch["max_leverage"] == 5.0
    assert ch["max_notional_usd"] == 5000.0


def test_sizing_order_stop_before_shares_documented():
    # The sizing rule (stop -> shares) lives in the strategy spec; the profile
    # must reference it so the numbers above stay auditable.
    cfg = _load()
    assert "vwap_pullback_continuation" == cfg["strategy"]["name"]
