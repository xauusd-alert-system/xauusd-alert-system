# Ruff Linting Policy

**Status:** active CI gate — `ruff check .` must pass on every push/PR.
**Config:** [`.ruff.toml`](../.ruff.toml) (repo root).
**CI job:** `lint` in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).
**Tool:** ruff >= 0.16.

## What is enforced (CI-blocking)

| Family | Scope | Rationale |
|---|---|---|
| `E` (pycodestyle errors) | Real syntax/whitespace errors (E9xx always on) | Correctness of the source tree |
| `F` (pyflakes) | Undefined names, unused imports, f-string issues | Catches real bugs (e.g. `F821`) |
| `W` (pycodestyle warnings) | Trailing whitespace, missing newline | Basic hygiene |
| `I` (isort) | Import sorting/ordering | Deterministic import layout |
| `N` (pep8-naming) | Class/function/variable naming | Consistent naming in new code |
| `UP` (pyupgrade) | Modernize syntax toward py312 | Keeps the codebase moving to modern Python |
| `B` (flake8-bugbear) | Likely bugs and design pitfalls | Bug prevention |

`line-length = 120`, `target-version = "py312"`, `src = ["."]`.

## What is ignored and why

| Rule | Count (baseline) | Why ignored |
|---|---|---|
| `E501` line-too-long | 2208 | Mandated by TZ; long lines are handled in review |
| `E402` module-import-not-at-top | 66 | Scripts manipulate `sys.path` before imports (intentional pattern) |
| `E702`/`E701` semicolon/one-line statements | 79 | Legacy scripts; cosmetic |
| `E731` lambda-assignment, `E741` ambiguous name | 17 | Legacy math code |
| `N803`/`N806` non-lowercase args/vars | 180 | Legacy domain math (XAU/USD formulas) uses uppercase locals |
| `N813`–`N818` camelCase import/exception naming | 23 | Legacy naming debt |
| `UP006`/`UP035`/`UP037`/`UP045` typing modernization | 318 | `Optional[X]`, `typing.List` etc. across legacy surface; gradual migration, not a mass rewrite |
| `UP009`/`UP015`/`UP030`–`UP031` misc legacy syntax | 70 | Cosmetic legacy habits |
| `F841` unused local | 45 | May reflect intent in scripts; cleanup separately |
| `B007`/`B904`/`B905`/`B011`/`B017` bugbear misc | 69 | Legacy patterns; each fix is behavior-sensitive |
| `UP042` str+Enum mixin → StrEnum | 6 | Serialization relies on str subclassing; migration is behavior-touching |
| `B023` loop-variable closure (regime/classifier.py) | 2 | Verified safe: lambda is consumed within the same iteration by `pandas.Series.map` |

## Per-file exceptions

| File | Rules | Reason |
|---|---|---|
| `simulation/mt5_shim/MetaTrader5/__init__.py` | `N999` | Shim must keep the exact module name of the real package it shadows |
| `model/trainer.py` | `F401` | `import lightgbm as lgb` inside try/except documents optional availability |
| `mt5_adapter/tests/test_client.py` | `B018` | Useless expression is intentional: asserts `AttributeError` on a missing constant |
| `realtime/dashboard.py` | `W293` | Legacy embedded HTML template; cosmetic |
| `realtime/prepost_metrics.py` | `N802` | Domain-specific bootstrap statistic name |
| `tests/builder.py` | `F821` | String forward-reference before a local import inside the factory |
| `execution/mt5_trader.py` | `I001`, `W292` | OWNER work-in-progress (uncommitted) — will be removed once the owner's diff lands |
| `execution/tests/test_trade_throttle.py` | `F401` | OWNER work-in-progress (uncommitted) — will be removed once the owner's diff lands |
| `tests/**` (and any `**/tests/**`) | `N802` | Test factories use CamelCase by convention |

## How to add an exception

1. **Single occurrence, clear reason** — add an inline `# noqa: RULE` with a
   short comment, e.g. `client.NO_SUCH_CONSTANT  # noqa: B018 — asserts AttributeError`.
2. **One file** — extend the `per-file-ignores` table in [`.ruff.toml`](../.ruff.toml)
   with a one-line justification.
3. **Whole codebase** — add the rule to the `ignore` list **only** with a comment
   stating the count and the deferral rationale, and record it in the table above.
4. Prefer fixing the issue over ignoring it: the ignore list is expected to
   **shrink** as legacy debt is paid down (that is the explicit direction of the
   refactor plan).

## Formatting (`ruff format`)

`ruff format --check .` currently fails on ~390 legacy files and therefore runs
in CI as **advisory** (`continue-on-error: true`). A repo-wide reformat is a
deliberate, standalone change that will land separately; once it does, the
advisory flag should be removed and formatting becomes a hard gate.

## Baseline (post auto-fix, 2026-08-28)

- Before: **1658** findings (963 auto-fixable) with a default-style run.
- After two safe auto-fix passes (`E,F,W,I` + the selected set): committed
  ~840 fixes across 250 files (import sorting, unused imports, `timezone.utc → UTC`,
  f-strings, redundant modes/parens, whitespace).
- Current: `ruff check .` → **0 errors** under the configured policy.
