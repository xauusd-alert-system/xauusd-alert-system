"""ТЗ 10.5–10.9 hardening guards (Часть 10 ТЗ):

* 10.11 — no interpolated VALUES in SQL execute f-strings (AST check:
  an interpolation enclosed in SQL single quotes is a value → flagged;
  interpolated identifiers — table/index names — are allowed);
* 10.11 — no ``shell=True`` in subprocess calls;
* 10.1/10.11 — ingest endpoint rejects oversized bodies (413).

SQL-identifier audit note (ТЗ 10.11): a manual grep of all
``execute(f"...")`` sites showed table names / PRAGMA / index names only;
all VALUE predicates use ``?`` parameters. The AST guard below enforces
the value-interpolation ban permanently.
"""
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SCAN_EXCLUDED_DIRS = {
    ".git", ".pytest_cache", "__pycache__", "node_modules", ".venv", "venv",
    "models", "output", "backup", "logs", "UI 3.7 flsah updated v3",
}
SELF = Path(__file__).resolve()


def _iter_py_files():
    for path in sorted(REPO.rglob("*.py")):
        if path.resolve() == SELF:
            continue
        rel = path.relative_to(REPO)
        if any(part in SCAN_EXCLUDED_DIRS for part in rel.parts):
            continue
        yield path


def _sql_value_interpolations(source: str, path: Path) -> list[str]:
    """Flag f-string SQL where an interpolation sits inside SQL quotes."""
    offenders: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return offenders
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        # parent call detection is not needed: any f-string whose literal
        # fragments look like SQL with an interpolation inside quotes.
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(("lit", value.value))
            else:
                parts.append(("expr", ""))
        text = "".join(p[1] for p in parts)
        if "SELECT" not in text.upper() and "INSERT" not in text.upper() \
                and "DELETE" not in text.upper() and "UPDATE" not in text.upper() \
                and "PRAGMA" not in text.upper() and "ALTER" not in text.upper():
            continue
        # walk fragments tracking SQL single-quote parity
        in_quote = False
        for idx, (kind, frag) in enumerate(parts):
            if kind == "expr":
                if in_quote:
                    expr_node = node.values[idx]
                    # Interpolations of ALL-CAPS module constants (TABLE,
                    # TABLE_NAME, ...) inside quotes are static identifiers
                    # (e.g. trigger RAISE messages) — not user data. Anything
                    # else interpolated inside SQL quotes is a value: banned.
                    names = [n for n in _names_in(expr_node) if not n.isupper()]
                    if names:
                        offenders.append(
                            f"{path.as_posix()}: interpolated SQL value inside "
                            f"quotes (name={names[0]!r})"
                        )
                        break
            else:
                in_quote = frag.count("'") % 2 == 1 if not in_quote else frag.count("'") % 2 == 0
    return offenders


def _names_in(node: ast.AST) -> list[str]:
    return [
        n.id for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    ]


def test_no_sql_value_interpolation_in_execute():
    offenders: list[str] = []
    for path in _iter_py_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        offenders.extend(_sql_value_interpolations(source, path.relative_to(REPO)))
    assert offenders == [], f"possible SQL value interpolation: {offenders}"


def test_no_shell_true():
    offenders = []
    pattern = "shell" + chr(61) + "True"  # avoid matching this file's own source
    for path in _iter_py_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        if pattern in source:
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], f"shell=True found: {offenders}"


def test_ingest_rejects_oversized_body(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import hashlib
    import hmac as hmac_mod
    from realtime.app import app, INGEST_MAX_BODY_BYTES

    monkeypatch.setenv("TRADE_LOG_DB_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("LEDGER_INGEST_TOKEN", "tok")
    monkeypatch.setenv("LEDGER_INGEST_SECRET", "sec")
    body = b"x" * (INGEST_MAX_BODY_BYTES + 1)
    sig = hmac_mod.new(b"sec", body, hashlib.sha256).hexdigest()
    client = TestClient(app)
    res = client.post(
        "/api/ledger/ingest", content=body,
        headers={"Authorization": "Bearer tok", "X-Ledger-Signature": sig},
    )
    assert res.status_code == 413
