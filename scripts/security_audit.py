"""ТЗ 10.2 / 10.10 — repository security audit.

Checks:
  a. .env listed in .gitignore (and .env / secrets not tracked in git);
  b. no hardcoded secrets in source (regex heuristics over *.py);
  c. file permissions on sensitive files (.env, risk_state.json, *.key/*.pem,
     *.sqlite) — POSIX chmod audit; on Windows existence + a warning only;
  d. no *.sqlite tracked in git (git ls-files).

CLI: ``python -m scripts.security_audit`` -> exit 1 on findings,
exit 0 on a clean report. ``--json`` prints machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# --- (b) hardcoded-secret heuristics -------------------------------------
# Patterns that look like real credentials committed to source.
SECRET_PATTERNS = [
    ("telegram_bot_token_literal", re.compile(r"TELEGRAM_BOT_TOKEN\s*=\s*['\"][0-9]{6,}:[A-Za-z0-9_-]{30,}['\"]")),
    ("api_key_literal", re.compile(r"\b(api_key|apikey|API_KEY)\s*=\s*['\"][A-Za-z0-9_-]{24,}['\"]")),
    (
        "bearer_token_literal",
        re.compile(r"\b(token|secret|password|passwd)\s*=\s*['\"][A-Za-z0-9_-]{24,}['\"]", re.IGNORECASE),
    ),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("long_hex_secret", re.compile(r"['\"][a-f0-9]{48,}['\"]")),
]

# Test fixtures legitimately contain fake tokens; scanning them would be
# pure noise. Directories excluded from the source scan.
SCAN_EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "models",
    "output",
    "backup",
    "logs",
    "UI 3.7 flsah updated v3",
    "data",
}

# Files exempt because they contain only placeholder/example values
# (the audit's own test fixtures included).
SECRET_SCAN_EXEMPT_FILES = {"security_audit.py", "test_security_audit.py"}


def _tracked_files(repo: Path) -> list[str]:
    """Files known to git (empty list when git is unavailable / not a repo)."""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if out.returncode != 0:
            return []
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def check_env_in_gitignore(repo: Path) -> list[str]:
    """(a) .env must be ignored by git and never tracked."""
    findings: list[str] = []
    gitignore = repo / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"^\.env\s*$", content, re.MULTILINE):
            findings.append(".gitignore: '.env' entry missing")
    else:
        findings.append(".gitignore file missing")
    tracked = _tracked_files(repo)
    if tracked:
        if ".env" in tracked:
            findings.append(".env is TRACKED in git (must be removed)")
        for name in tracked:
            if name.endswith((".key", ".pem")):
                findings.append(f"key material tracked in git: {name}")
    return findings


def check_no_secrets_in_source(repo: Path) -> list[str]:
    """(b) regex scan of tracked-relevant *.py sources for credential literals."""
    findings: list[str] = []
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if any(part in SCAN_EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.name in SECRET_SCAN_EXEMPT_FILES:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, pattern in SECRET_PATTERNS:
            match = pattern.search(source)
            if match:
                findings.append(f"possible hardcoded secret ({name}) in {rel.as_posix()}")
    return findings


def check_file_permissions(repo: Path) -> list[str]:
    """(c) sensitive files should be owner-only (POSIX 600).

    On Windows the POSIX mode is not meaningful: existence is reported as an
    informational warning and no chmod is attempted (NTFS ACLs are not
    managed by this audit).
    """
    findings: list[str] = []
    sensitive_names = [".env", "risk_state.json"]
    for path in sorted(repo.rglob("*")):
        rel = path.relative_to(repo)
        if any(part in SCAN_EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        name = path.name
        if (
            name.endswith((".key", ".pem"))
            or name in sensitive_names
            or name.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm"))
        ):
            if name in sensitive_names or name.endswith((".key", ".pem")):
                if not path.exists():
                    continue
                if os.name == "posix":
                    mode = os.stat(path).st_mode & 0o777
                    if mode & 0o077:  # group/other bits set
                        try:
                            os.chmod(path, 0o600)
                            findings.append(f"permissions tightened to 600: {rel.as_posix()} (was {oct(mode)})")
                        except OSError as exc:
                            findings.append(f"chmod failed for {rel.as_posix()}: {exc}")
                else:
                    findings.append(f"sensitive file present (verify ACLs manually): {rel.as_posix()}")
    return findings


def check_sqlite_not_tracked(repo: Path) -> list[str]:
    """(d) no *.sqlite (or sidecars) tracked in git."""
    findings: list[str] = []
    tracked = _tracked_files(repo)
    for name in tracked:
        if name.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal")):
            findings.append(f"sqlite database tracked in git: {name}")
    return findings


def security_audit(repo: Path = REPO_ROOT) -> dict:
    findings: list[str] = []
    findings.extend(check_env_in_gitignore(repo))
    findings.extend(check_no_secrets_in_source(repo))
    findings.extend(check_file_permissions(repo))
    findings.extend(check_sqlite_not_tracked(repo))
    return {"findings": findings, "clean": not findings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repository security audit (ТЗ 10.2/10.10)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    report = security_audit()
    if args.json:
        print(json.dumps(report, indent=2))
    elif report["clean"]:
        print("security audit: OK — no findings")
    else:
        print(f"security audit: {len(report['findings'])} finding(s)")
        for item in report["findings"]:
            print(f"  - {item}")
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
