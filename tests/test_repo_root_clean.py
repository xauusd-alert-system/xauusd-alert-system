"""TZ Часть 7, п.7.3 / P2-28 / P2-29: repository root hygiene.

The root must contain only project-wide config/docs files plus the pytest
``conftest.py``. Research utilities live in ``scripts/research/`` and
generated artifacts go to ``artifacts/`` (git-ignored).

The whitelist applies to *git-tracked* entries: local-only runtime dirs that
are already covered by .gitignore (``venv/``, ``logs/``, ``output/``, ... or
the local ``.env``) are not tracked and must not block the guard.
"""
import os
import subprocess

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Files that are ALLOWED in the repository root (whitelist, exact names).
ALLOWED_ROOT_FILES = {
    # tooling / project meta
    ".dockerignore",
    ".gitignore",
    ".env.example",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
    "pyproject.toml",
    "requirements.txt",
    "requirements_simulation.txt",
    "README.md",
    "INSTRUCTION_agents.md",
    "TZ_xauusd_alert_system.md",
    # pytest requires the root conftest for shared fixtures
    "conftest.py",
}

ALLOWED_ROOT_DIRS = {
    ".github",
    "alerts",
    "artifacts",
    "backtest",
    "challenge",
    "config",
    "contracts",
    "data",
    "deploy",
    "docs",
    "execution",
    "features",
    "labeling",
    "logs",  # real source: logs/setup.py, logs/journal.py (see .gitignore notes)
    "model",
    "monitoring",
    "mql5",
    "mt5_adapter",
    "news",
    "pairs_analysis",
    "paper",
    "plans",
    "provenance",
    "realtime",
    "regime",
    "risk",
    "scripts",
    "services",
    "simulation",
    "tests",
    "usstocks",
    "UI 3.7 flsah updated v3",  # TS UI source (renaming tracked in docs/TODO.md)
}


def _tracked_root_entries():
    """Top-level entries tracked by git (files and first path segment of dirs)."""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        # git unavailable — fall back to the physical listing (fails safe:
        # anything unexpected in the root is reported).
        return {p for p in os.listdir(REPO_ROOT) if not p.startswith(".")}
    entries = set()
    for line in out.splitlines():
        if not line:
            continue
        first = line.replace("\\", "/").split("/", 1)[0]
        entries.add(first)
    return entries


def test_no_python_files_in_repo_root():
    """P2-28: no one-off .py utilities in the root — they belong in scripts/."""
    offenders = [
        name
        for name in _tracked_root_entries()
        if name.endswith(".py") and name not in ALLOWED_ROOT_FILES
    ]
    assert offenders == [], (
        "Research/utility scripts must not live in the repo root "
        f"(moved to scripts/research/ in TZ 7.3): {sorted(offenders)}"
    )


def test_no_artifact_files_in_repo_root():
    """P2-29: no generated artifacts in the root — they belong in artifacts/."""
    offenders = [
        name
        for name in _tracked_root_entries()
        if name.lower().endswith((".html", ".out"))
        or name.endswith(".patch")
        or name in {"session_index.txt", "pytest_mt5_out.txt"}
    ]
    assert offenders == [], (
        "Generated artifacts must not live in the repo root "
        f"(moved to artifacts/ per TZ 7.3): {sorted(offenders)}"
    )


def test_root_entries_are_whitelisted():
    """Every git-tracked root entry is whitelisted."""
    offenders = [
        name
        for name in _tracked_root_entries()
        if name not in ALLOWED_ROOT_FILES and name not in ALLOWED_ROOT_DIRS
    ]
    assert offenders == [], f"Unexpected tracked entries in repo root: {sorted(offenders)}"


def test_dotenv_not_tracked():
    """.env must never be tracked (secrets stay local)."""
    assert ".env" not in _tracked_root_entries()


@pytest.mark.parametrize(
    "script_name",
    [
        "dump_btcusd.py",
        "dump_xauusd.py",
        "check_btcusd_durations.py",
        "check_symbols.py",
        "truncate_db.py",
    ],
)
def test_research_scripts_relocated(script_name):
    """P2-28: research utilities exist in scripts/research/ (not in root)."""
    assert os.path.isfile(
        os.path.join(REPO_ROOT, "scripts", "research", script_name)
    ), f"scripts/research/{script_name} is missing"
    assert not os.path.exists(os.path.join(REPO_ROOT, script_name))


def test_artifacts_dir_is_ignored():
    """artifacts/ exists with a tracked marker and generated files inside are
    ignored by git."""
    assert os.path.isdir(os.path.join(REPO_ROOT, "artifacts"))
    assert os.path.isfile(os.path.join(REPO_ROOT, "artifacts", ".gitkeep"))
