# Ruff Autofix Results

**Date:** 2026-08-28
**Branch:** `refactor/master-plan`
**Tool:** ruff 0.16.5
**Config:** [`../.ruff.toml`](../.ruff.toml) (repo root)
**CI gate:** `ruff check .` in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) — must pass on every push/PR.

## Summary

| Metric | Value |
|---|---:|
| Findings before (default-style run) | **1658** |
| Of which safely auto-fixable | **963** |
| Auto-fixed automatically (committed) | **~840** |
| Remaining under configured policy (`ruff check .`) | **0** |
| Remaining raw `E,F,W,I` (ignored legacy debt) | **387** |

## What was auto-fixed (already in history)

The safe-autofix pass was executed and committed in two commits:

1. **`7f50dc9` — `style: auto-fix safe ruff issues (E,F,W,I)`**
   - 248 files changed, +879 / −780.
   - Import sorting (`I`), unused imports/names (`F`), whitespace/newline hygiene
     (`W`), pycodestyle fixes (`E`), f-string/`redundant-mode` fixes.

2. **`72c2577` — `style: auto-fix ruff UP017 (timezone.utc -> datetime.UTC) across legacy modules`**
   - 64 files changed, +211 / −209.
   - A selected `UP` modernization that is provably safe (no runtime behavior
     change), applied before the general `UP` debt was deferred.

`ruff check .` (configured policy) reports **0 errors** as a result.

## Why nothing more was auto-fixed on this run

A fresh `ruff check . --fix --select E,F,W,I` was run. It changed **no files**:
the remaining 387 raw `E,F,W,I` findings all fall into rules that are already
listed in `.ruff.toml` as documented legacy debt and **have no safe fixes**
(`No fixes available`; 47 of them are only reachable via `--unsafe-fixes`).

Running `--unsafe-fixes` was deliberately **not** done: those fixes (e.g.
removing unused locals that may reflect intent (`F841`), splitting
semicolon-joined statements / moving `sys.path` imports (`E402`, `E702`))
touch behavior- or intent-sensitive legacy code and contradict the documented
policy in [`RUFF_POLICY.md`](RUFF_POLICY.md) ("Prefer fixing over ignoring …
do not gate CI on behavior-touching changes").

## Remaining raw E/F/W/I breakdown (all ignored in .ruff.toml)

| Rule | Count | Status in `.ruff.toml` |
|---|---:|---|
| `E501` line-too-long | 180 | ignored (mandated by TZ review, not lint) |
| `E702` multiple-statements semicolon | 68 | ignored (legacy scripts, cosmetic) |
| `E402` module-import-not-at-top | 66 | ignored (sys.path pattern in scripts) |
| `F841` unused variable | 45 | ignored (may reflect intent; cleanup separately) |
| `E741` ambiguous variable name | 15 | ignored (legacy math) |
| `E701` multiple-statements colon | 11 | ignored (legacy scripts) |
| `E731` lambda assignment | 2 | ignored (legacy math) |
| **Total** | **387** | — |

These are tracked in the future work backlog (Task 5 "Исправить 695 оставшихся
ruff проблем") — a separate, manual effort per rule.

## Re-run locally

```bash
# configured policy (CI-equivalent):
ruff check .

# raw E/F/W/I view (includes ignored legacy debt):
ruff check . --select E,F,W,I --statistics
```
