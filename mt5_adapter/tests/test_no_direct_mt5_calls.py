"""Guard test: no direct MetaTrader5 usage outside mt5_adapter/ (ТЗ 8.6).

Scans the production source trees (data/, execution/, scripts/, realtime/,
alerts/) for ``import MetaTrader5`` / ``import mt5`` and direct ``mt5.<attr>``
accesses. Everything must go through :class:`mt5_adapter.client.MT5Client`.

A small, documented WHITE_LIST of files that cannot be converted yet (or must
stay raw by design) is kept below with TODO comments. Goal: shrink it to zero.
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Trees scanned by the guard.
SCAN_DIRS = ("data", "execution", "scripts", "realtime", "alerts")

# Excluded sub-trees: tests and mocks are allowed to fake the raw module.
EXCLUDED_PARTS = ("tests", "__pycache__", "mt5_adapter")

# ---------------------------------------------------------------------------
# White-list: files still touching raw MetaTrader5 (each entry needs a TODO).
# ---------------------------------------------------------------------------
WHITE_LIST: dict[str, str] = {
    # Simulation entry point: must inject the virtual MT5 shim onto sys.path
    # BEFORE the protected modules import it — by design it owns the raw import.
    "scripts/run_simulation.py":
        "TODO(8.6): shim entry point; sys.path injection requires raw import",
    # Same shim-injection role as run_simulation (virtual-mode bot entry).
    "scripts/run_bot.py":
        "TODO(8.6): shim entry point; sys.path injection requires raw import",
    # Ops/admin bot: reads MT5 status ad-hoc; convert to MT5Client when the
    # admin bot gets a DI path (low risk, cosmetic).
    "scripts/telegram_admin.py":
        "TODO(8.6): convert status reads to MT5Client",
    # Alerts control bot: convert to MT5Client once control_bot gets a DI
    # constructor parameter (protects closeall path).
    "alerts/control_bot.py":
        "TODO(8.6): convert /positions and /closeall to MT5Client",
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+(?:MetaTrader5|mt5)\b|from\s+(?:MetaTrader5|mt5)\b)")
_ATTR_RE = re.compile(r"\bmt5\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)")


def _iter_py_files():
    for d in SCAN_DIRS:
        root = PROJECT_ROOT / d
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in EXCLUDED_PARTS for part in path.parts):
                continue
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            yield rel, path


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _effective_line(lines: list[str], idx: int) -> str:
    """Concatenate a statement across explicit backslash continuations so a
    multi-line ``mt5.\n    symbol_info(...)`` call is still detected."""
    line = lines[idx]
    out = line
    j = idx
    while out.rstrip().endswith("\\") and j + 1 < len(lines):
        j += 1
        out = out.rstrip()[:-1] + " " + lines[j].strip()
    return out


def test_no_direct_mt5_imports_outside_adapter():
    offenders = []
    for rel, path in _iter_py_files():
        allowed = rel in WHITE_LIST
        for idx, line in enumerate(_lines(path)):
            if _IMPORT_RE.match(line):
                if not allowed:
                    offenders.append(f"{rel}:{idx + 1}: {line.strip()}")
    assert not offenders, (
        "Direct MetaTrader5 imports found outside mt5_adapter/ "
        "(convert to mt5_adapter.MT5Client):\n" + "\n".join(offenders))


_SELF_MT5_RE = re.compile(r"\bself\.mt5\s*\.")


def test_no_direct_mt5_attr_access_outside_adapter():
    """Direct ``mt5.*`` usage is only allowed when the handle demonstrably
    comes from the adapter layer:

    * ``mt5 = get_mt5_module()`` (mt5_adapter.lazy) in the same file; or
    * ``self.mt5`` — a handle injected through a constructor parameter
      (dependency injection, ТЗ 8.6).

    Anything else (a bare ``mt5.`` in a file with no adapter reference) means
    the file bypasses the adapter."""
    offenders = []
    for rel, path in _iter_py_files():
        allowed = rel in WHITE_LIST
        lines = _lines(path)
        source = path.read_text(encoding="utf-8", errors="replace")
        adapter_based = "from mt5_adapter" in source or "mt5_adapter" in source
        for idx, line in enumerate(lines):
            eff = _effective_line(lines, idx)
            if _IMPORT_RE.match(line):
                continue
            for match in _ATTR_RE.finditer(eff):
                prefix = eff[:match.start()]
                is_injected = prefix.rstrip().endswith("self.")
                if is_injected or adapter_based:
                    continue  # adapter-resolved / DI-injected handle
                if not allowed:
                    offenders.append(f"{rel}:{idx + 1}: {line.strip()}")
                break
    assert not offenders, (
        "Direct `mt5.*` attribute access found outside mt5_adapter/ "
        "(use mt5_adapter.MT5Client or DI):\n" + "\n".join(offenders))


def test_white_list_entries_exist():
    """Every white-listed file must still exist; drop stale entries."""
    missing = [rel for rel in WHITE_LIST
               if not (PROJECT_ROOT / rel).exists()]
    assert not missing, f"Stale white-list entries (file gone): {missing}"


def test_white_list_is_minimal_and_documented():
    """Each white-list entry carries a TODO comment (the goal is zero)."""
    for rel, note in WHITE_LIST.items():
        assert note.startswith("TODO"), (
            f"White-list entry {rel!r} must carry a TODO rationale")


def test_adapter_package_is_exempt():
    """mt5_adapter itself legitimately imports MetaTrader5 (lazily)."""
    lazy = PROJECT_ROOT / "mt5_adapter" / "lazy.py"
    assert "import MetaTrader5" in lazy.read_text(encoding="utf-8")
