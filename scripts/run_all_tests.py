"""Run the FULL test suite as CI.

Collects every ``test_*.py`` that is part of the repo-wide pytest suite:

  * every ``<package>/tests/test_*.py`` directory (alerts, backtest,
    challenge, contracts, data, execution, features, labeling, model, paper,
    realtime, regime, scripts, plus the repo-root ``tests/``);
  * the ``challenge/manual`` unit tests (real unittest classes, 77 passing);

EXCLUDES the standalone scripts that are not part of the discoverable suite:

  * ``scripts/test_crypto_regime_aug24.py``,
  * ``scripts/test_crypto_regime_standalone.py``;

Those two are top-level ``main()`` scripts that hit live Hash Hedge / UTEx
APIs and need a real token file — running them in CI would either fail on the
network or, worse, send requests. Plain ``pytest`` (testpaths = ["."]) would
try to import them, so this runner explicitly ignores them.

The run is a single pytest invocation over the discovered directories, so
fixtures, markers and config resolve exactly as they do when a developer runs
``pytest <dir>`` locally. A per-directory breakdown and a grand total are
written to ``logs/run_all_tests.log`` (and echoed). Exit 0 = all green;
1 = any failure/error.

Usage:
    python -m scripts.run_all_tests [--keep-going] [--pytest-args "-q"]

    --keep-going   run the whole set (default: quick-fail at the first
                   failure to save CI minutes).
    --pytest-args  extra args forwarded to pytest.

Read-only: never modifies sources or the DB.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, ROOT)

# Packages that own a `tests/` dir. Discovery also scans for any new package,
# so this set is the default, not a hard gate.
PACKAGE_NAMES = [
    "alerts", "backtest", "challenge", "contracts", "data", "execution",
    "features", "labeling", "model", "paper", "realtime", "regime", "scripts",
]
# Real unit tests that live OUTSIDE a `tests/` dir.
EXTRA_TEST_DIRS = ["challenge/manual"]
# Root-level integration tests.
ROOT_TESTS_DIR = "tests"

# Standalone scripts excluded from CI (external-API / `main()` runners).
STANDALONE_IGNORES = [
    "scripts/test_crypto_regime_aug24.py",
    "scripts/test_crypto_regime_standalone.py",
]

LOG_PATH = os.path.join("logs", "run_all_tests.log")


def _discover_test_dirs() -> list[str]:
    """ROOT/<pkg>/tests for every present package, the root tests/, and the
    extra manual-test dirs. Missing dirs are skipped silently."""
    dirs = []
    for name in PACKAGE_NAMES:
        d = os.path.join(ROOT, name, "tests")
        if os.path.isdir(d):
            dirs.append(d)
    root_tests = os.path.join(ROOT, ROOT_TESTS_DIR)
    if os.path.isdir(root_tests):
        dirs.append(root_tests)
    for extra in EXTRA_TEST_DIRS:
        d = os.path.join(ROOT, extra)
        if os.path.isdir(d):
            dirs.append(d)
    return dirs


def _pytest_command(test_dirs: list[str], extra_args: str, keep_going: bool) -> list[str]:
    cmd = [
        sys.executable, "-m", "pytest",
        "-p", "no:cacheprovider",   # CI: no stale .pytest_cache
        "--durations=10",           # surface the ten slowest
    ]
    if not keep_going:
        cmd += ["--maxfail", "1"]
    if extra_args.strip():
        cmd += extra_args.strip().split()
    cmd += test_dirs
    for rel in STANDALONE_IGNORES:
        cmd += ["--ignore", os.path.join(ROOT, rel)]
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-going", action="store_true",
        help="run the full set even if a directory fails (default: quick-fail)",
    )
    parser.add_argument(
        "--pytest-args", default="",
        help='extra args forwarded to pytest, e.g. "--no-header -q"',
    )
    args = parser.parse_args()

    test_dirs = _discover_test_dirs()
    cmd = _pytest_command(test_dirs, args.pytest_args, args.keep_going)

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    started = time.time()

    print(
        f"[run_all_tests] {len(test_dirs)} test dirs, "
        f"{len(STANDALONE_IGNORES)} standalone excludes"
    )
    print(f"[run_all_tests] invoking: {' '.join(cmd)}")

    # Single pass: pytest streams into the log (transcript incl. per-dir dots
    # and --durations) and is mirrored to stdout live. No extra re-run passes.
    with open(LOG_PATH, "w", encoding="utf-8") as log:
        log.write(f"run_all_tests started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.write(f"command: {' '.join(cmd)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
            log.write(line)
        global_rc = proc.wait()
        elapsed = time.time() - started
        log.write(f"\nrun_all_tests finished in {elapsed:.1f}s (exit={global_rc})\n")

    verdict = "ALL GREEN" if global_rc == 0 else "FAILED"
    print(
        f"\n[run_all_tests] {verdict} (exit={global_rc}) "
        f"in {time.time() - started:.1f}s — log: {LOG_PATH}"
    )
    return global_rc


if __name__ == "__main__":
    sys.exit(main())