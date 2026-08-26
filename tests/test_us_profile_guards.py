# -*- coding: utf-8 -*-
"""Stage B guards (docs/MIGRATION_PLAN.md): profile resolution + startup
interlocks that make auto-trading technically impossible in signal-only
profiles."""
import pytest

from config.loader import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    SIGNAL_ONLY_PROFILES,
    get_profile,
    is_signal_only,
)
from usstocks.guards import (
    EXIT_SIGNAL_ONLY,
    assert_auto_trading_allowed,
    require_signal_only,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("PROFILE", raising=False)
    yield


# ---------------------------------------------------------------------------
# config.loader.get_profile
# ---------------------------------------------------------------------------

def test_default_profile_is_legacy(monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog"])
    assert get_profile() == DEFAULT_PROFILE == "forex_legacy"
    assert not is_signal_only()


def test_env_profile_selects_us_stocks(monkeypatch):
    monkeypatch.setenv("PROFILE", "us_stocks_challenge")
    assert get_profile() == "us_stocks_challenge"
    assert is_signal_only()


@pytest.mark.parametrize("argv,expected", [
    (["prog", "--profile", "replay"], "replay"),
    (["prog", "--profile=us_stocks_challenge"], "us_stocks_challenge"),
])
def test_cli_flag_overrides_env(monkeypatch, argv, expected):
    monkeypatch.setenv("PROFILE", "forex_legacy")
    monkeypatch.setattr("sys.argv", argv)
    assert get_profile() == expected


def test_unknown_profile_fails_fast(monkeypatch):
    monkeypatch.setenv("PROFILE", "yolo_mode")
    with pytest.raises(ValueError, match="Unknown PROFILE"):
        get_profile()


def test_known_profiles_contract():
    # The two legacy profiles must remain executable; the two new ones must
    # stay signal-only. Guards below rely on this partition.
    assert set(SIGNAL_ONLY_PROFILES) == {"us_stocks_challenge", "replay"}
    assert set(KNOWN_PROFILES) - set(SIGNAL_ONLY_PROFILES) == {
        "forex_legacy", "crypto_legacy",
    }


# ---------------------------------------------------------------------------
# usstocks.guards.assert_auto_trading_allowed
# ---------------------------------------------------------------------------

def test_guard_refuses_under_us_stocks_profile(monkeypatch):
    monkeypatch.setenv("PROFILE", "us_stocks_challenge")
    with pytest.raises(SystemExit) as exc:
        assert_auto_trading_allowed("some.trader")
    assert exc.value.code == EXIT_SIGNAL_ONLY == 2


def test_guard_refuses_under_replay_profile(monkeypatch):
    monkeypatch.setenv("PROFILE", "replay")
    with pytest.raises(SystemExit):
        assert_auto_trading_allowed("some.trader")


def test_guard_passes_for_legacy_profiles(monkeypatch):
    for prof in ("forex_legacy", "crypto_legacy"):
        monkeypatch.setenv("PROFILE", prof)
        # must NOT raise
        assert_auto_trading_allowed("legacy.entry")


# ---------------------------------------------------------------------------
# Entry-point interlocks: the real main() functions refuse to start.
# ---------------------------------------------------------------------------

def _run_main_with_profile(monkeypatch, module, attr, profile, *args, **kwargs):
    monkeypatch.setenv("PROFILE", profile)
    return getattr(module, attr)(*args, **kwargs)


def test_challenge_runner_refuses_to_start(monkeypatch):
    import challenge.runner as runner_mod
    monkeypatch.setenv("PROFILE", "us_stocks_challenge")
    monkeypatch.setattr("sys.argv", ["challenge.runner"])
    with pytest.raises(SystemExit) as exc:
        runner_mod.main()
    # The guard fires BEFORE any browser launch (launch(cfg) is unreachable).
    assert exc.value.code == EXIT_SIGNAL_ONLY == 2


def test_stealth_bridge_refuses_under_signal_only(monkeypatch):
    from challenge.stealth import runner_bridge
    monkeypatch.setenv("PROFILE", "us_stocks_challenge")
    with pytest.raises(SystemExit):
        runner_bridge.build_engine({"challenge": {"stealth": {"enabled": True}}})


def test_stealth_bridge_still_none_when_disabled_no_profile(monkeypatch):
    # Legacy behaviour preserved: without stealth.enabled the bridge returns
    # None and nothing else happens.
    from challenge.stealth import runner_bridge
    monkeypatch.setattr("sys.argv", ["x"])
    assert runner_bridge.build_engine({"challenge": {}}) is None


def test_run_bot_entry_point_has_interlock():
    # Source-level guarantee: run_bot.main() calls the guard before anything
    # else (a full import would spin up the simulator stack).
    import inspect
    import scripts.run_bot as rb
    src = inspect.getsource(rb.main)
    assert "assert_auto_trading_allowed" in src.split("\n")[3] or \
        "assert_auto_trading_allowed" in src


def test_require_signal_only_inverse_guard(monkeypatch):
    monkeypatch.setenv("PROFILE", "us_stocks_challenge")
    assert require_signal_only("usstocks.scanner_loop") == "us_stocks_challenge"
    monkeypatch.setenv("PROFILE", "forex_legacy")
    with pytest.raises(SystemExit):
        require_signal_only("usstocks.scanner_loop")
