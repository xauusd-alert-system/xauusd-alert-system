"""Unit tests for the CI runner scripts/run_all_tests.py.

Only the pure-logic parts are tested (discovery, ignore-command construction,
pytest arg forwarding) — never an actual subprocess run of the whole suite.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import scripts.run_all_tests as rat


def test_discover_includes_every_package_tests_and_manual():
    dirs = rat._discover_test_dirs()
    rel = [os.path.relpath(d, rat.ROOT) for d in dirs]
    # Every package with a tests/ dir is discovered.
    for pkg in ("alerts", "backtest", "challenge", "execution", "realtime", "scripts"):
        assert os.path.join(pkg, "tests") in rel, f"missing {pkg}/tests"
    # Root tests/ and the manual unit tests are included.
    assert os.path.join("tests") in rel or "tests" in rel
    assert os.path.join("challenge", "manual") in rel
    # All discovered dirs are non-empty test homes (sanity against a renamed pkg).
    for d in dirs:
        assert os.path.isdir(d)


def test_standalone_scripts_flagged_for_ignore():
    # The two external-API runners must be explicitly excluded.
    assert "scripts/test_crypto_regime_aug24.py" in rat.STANDALONE_IGNORES
    assert "scripts/test_crypto_regime_standalone.py" in rat.STANDALONE_IGNORES


def test_command_ignores_all_standalone_scripts():
    dirs = ["alerts/tests", "realtime/tests"]
    cmd = rat._pytest_command(dirs, extra_args="", keep_going=True)
    for rel in rat.STANDALONE_IGNORES:
        assert "--ignore" in cmd
        assert os.path.join(rat.ROOT, rel) in cmd


def test_command_maxfail_when_not_keep_going():
    cmd = rat._pytest_command(["a/tests"], extra_args="", keep_going=False)
    assert "--maxfail" in cmd

    cmd_keep = rat._pytest_command(["a/tests"], extra_args="", keep_going=True)
    assert "--maxfail" not in cmd_keep


def test_command_forwards_extra_pytest_args():
    cmd = rat._pytest_command(["a/tests"], extra_args="-q --no-header", keep_going=True)
    assert "-q" in cmd
    assert "--no-header" in cmd


def test_command_uses_no_cacheprovider_for_ci():
    cmd = rat._pytest_command(["a/tests"], extra_args="", keep_going=True)
    assert "-p" in cmd and "no:cacheprovider" in cmd