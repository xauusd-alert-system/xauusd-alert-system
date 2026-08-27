"""ТЗ 6.7: Docker / docker-compose configuration validity tests.

Pure lint checks — no docker daemon is required (CI has no docker):

    - Dockerfile exists, is python:3.12-slim based, pins WORKDIR /app and the
      paper/alerts entrypoint ``python -m scripts.run_bot``;
    - .dockerignore excludes secrets and local state (.env, *.sqlite, logs/,
      backups/, .git, .pytest_cache, models/);
    - docker-compose.yml parses as YAML, defines the ``bot`` service with
      ``env_file: .env`` and volume mounts, and every declared healthcheck
      command is well-formed;
    - requirements.txt keeps MetaTrader5 behind the Windows platform marker
      (guarantee that the Linux image / CI build still installs).

These tests intentionally re-parse the files instead of invoking docker so
they run in every environment.
"""
from __future__ import annotations

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"


# ------------------------------------------------------------------ Dockerfile

def test_dockerfile_exists_and_targets_python_312_slim():
    assert DOCKERFILE.exists(), "Dockerfile missing from repository root"
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^FROM\s+python:3\.12-slim", text, re.MULTILINE), (
        "Dockerfile must be based on python:3.12-slim"
    )


def test_dockerfile_entrypoint_is_paper_bot():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(
        r'^CMD\s+\["python",\s*"-m",\s*"scripts\.run_bot"\]',
        text, re.MULTILINE,
    ), "CMD must launch the paper/alerts bot entrypoint"
    assert "WORKDIR /app" in text


# ---------------------------------------------------------------- .dockerignore

@pytest.mark.parametrize("pattern", [
    ".env", "*.sqlite", "logs/", "backups/", ".git", ".pytest_cache", "models/",
])
def test_dockerignore_excludes_secrets_and_state(pattern):
    assert DOCKERIGNORE.exists(), ".dockerignore missing"
    lines = [ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()]
    assert pattern in lines, f".dockerignore must exclude {pattern!r}"


# ------------------------------------------------------------ docker-compose

def test_docker_compose_is_valid_yaml_with_bot_service():
    assert COMPOSE_FILE.exists(), "docker-compose.yml missing"
    data = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    services = data.get("services")
    assert isinstance(services, dict) and "bot" in services

    bot = services["bot"]
    assert bot.get("env_file") == ".env", "bot must load secrets from .env"
    volumes = " ".join(bot.get("volumes", []))
    for mount in ("./data", "./logs", "./config"):
        assert mount in volumes, f"bot must mount {mount}"

    # Every service with a healthcheck must have a non-empty test command.
    for name, svc in services.items():
        hc = svc.get("healthcheck")
        if hc is not None:
            assert hc.get("test"), f"service {name}: empty healthcheck test"


def test_docker_compose_healthchecks_reference_correct_endpoints():
    data = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = data["services"]
    # dashboard probes the enriched /api/health (TZ 6.3); sidecars probe /health.
    dashboard_hc = " ".join(services["dashboard"]["healthcheck"]["test"])
    assert "/api/health" in dashboard_hc
    for name in ("ledger_bridge", "news_feed"):
        hc = " ".join(services[name]["healthcheck"]["test"])
        assert "/health" in hc, f"{name} must probe GET /health"
    # bot serves no HTTP endpoint: process-liveness via /proc/1/cmdline.
    bot_hc = " ".join(services["bot"]["healthcheck"]["test"])
    assert "/proc/1/cmdline" in bot_hc


# ------------------------------------------------------------- requirements

def test_metatrader5_is_windows_only_in_requirements():
    """The Linux image / CI must be able to install requirements.txt."""
    text = REQUIREMENTS.read_text(encoding="utf-8")
    mt5_lines = [ln for ln in text.splitlines()
                 if ln.strip().lower().startswith("metatrader5")]
    assert mt5_lines, "MetaTrader5 pin missing from requirements.txt"
    for ln in mt5_lines:
        assert 'platform_system == "Windows"' in ln, (
            "MetaTrader5 must keep its Windows platform marker so the "
            f"Docker build (Linux) can install the rest of the stack: {ln!r}"
        )
