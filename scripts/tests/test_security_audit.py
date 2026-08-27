"""ТЗ 10.2 — secrets protection: gitignore hardening, secret scanning,
file-permission audit, sqlite tracking."""
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.security_audit import (  # noqa: E402
    check_env_in_gitignore,
    check_file_permissions,
    check_no_secrets_in_source,
    check_sqlite_not_tracked,
    security_audit,
)

REPO = Path(__file__).resolve().parents[2]


def test_env_in_gitignore():
    """.env (and key material) must be git-ignored in the real repo."""
    assert check_env_in_gitignore(REPO) == []


def test_no_secrets_in_source():
    """The real repository must not contain committed credential literals."""
    findings = check_no_secrets_in_source(REPO)
    # report only; real secrets must not exist
    assert findings == [], findings


def test_security_audit_detects_missing_env(tmp_path):
    """A tmp repo without .env in .gitignore is flagged by the audit."""
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    report = security_audit(tmp_path)
    assert any(".env" in f and "gitignore" in f.lower() for f in report["findings"])
    assert report["clean"] is False


def test_security_audit_detects_hardcoded_secret(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        'API_KEY = "abcd1234abcd1234abcd1234"\n', encoding="utf-8"
    )
    findings = check_no_secrets_in_source(tmp_path)
    assert any("api_key_literal" in f for f in findings)


def test_security_audit_ignores_env_var_lookup(tmp_path):
    """Reading os.environ / get_env is NOT a hardcoded secret."""
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text(
        'import os\nTOKEN = os.environ.get("API_AUTH_TOKEN")\n'
        'SECRET = get_env("LEDGER_INGEST_SECRET", default=None)\n',
        encoding="utf-8",
    )
    assert check_no_secrets_in_source(tmp_path) == []


def test_security_audit_posix_permission_tightening(tmp_path):
    """On POSIX an over-permissive .env is chmod'ed to 600 and reported."""
    env_file = tmp_path / ".env"
    env_file.write_text("API_AUTH_TOKEN=x\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    os.chmod(env_file, 0o644)
    findings = check_file_permissions(tmp_path)
    if os.name == "posix":
        assert any("600" in f for f in findings)
        assert (os.stat(env_file).st_mode & 0o777) == 0o600
    else:
        # Windows: existence-only warning
        assert any(".env" in f for f in findings)


def test_sqlite_not_tracked():
    """No *.sqlite (or sidecars) tracked in the real repo."""
    assert check_sqlite_not_tracked(REPO) == []


def test_security_audit_detects_tracked_sqlite(tmp_path, monkeypatch):
    """A git repo with a tracked .sqlite is flagged."""
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / "data.sqlite").write_bytes(b"x")
    def _fake_tracked(repo):
        return ["data.sqlite"] if repo == tmp_path else []
    monkeypatch.setattr("scripts.security_audit._tracked_files", _fake_tracked)
    assert check_sqlite_not_tracked(tmp_path) == ["sqlite database tracked in git: data.sqlite"]


def test_cli_exit_code(monkeypatch):
    from scripts import security_audit as mod
    monkeypatch.setattr(mod, "security_audit", lambda repo=REPO: {"findings": [], "clean": True})
    assert mod.main([]) == 0
    monkeypatch.setattr(
        mod, "security_audit",
        lambda repo=REPO: {"findings": ["demo finding"], "clean": False},
    )
    assert mod.main([]) == 1
