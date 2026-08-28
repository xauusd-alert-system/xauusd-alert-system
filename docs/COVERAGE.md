# Coverage Report

**Date:** 2026-08-28
**Branch:** `refactor/master-plan`
**Run:** `pytest -q --cov=. --cov-report=term --cov-report=html --cov-report=json` with [`.coveragerc`](../.coveragerc)
**Test suite:** 1443 passed / 1 skipped (identical to the plain run — coverage instrumentation changes no outcomes).
**Total coverage: 57.8%** (12 889 / 22 288 statements).

Measurement excludes non-code and auxiliary surfaces via `.coveragerc`:
`tests/`, `*/tests/`, `docs/`, `plans/`, `scripts/research/`, one-off tooling
packages (`backtest/`, `challenge/`, `labeling/`, `news/`, `pairs_analysis/`,
`paper/`, `usstocks/`), the UI bundle, `deploy/`, `mql5/` and `simulation/`
(MT5 shim stands in for the real broker API; its behavior is asserted by the
suite, but measuring it would not reflect production execution paths).

## Coverage by package

| Package | Statements | Covered | Coverage |
|---|---:|---:|---:|
| config | 314 | 301 | 95.9% |
| contracts | 192 | 185 | 96.4% |
| provenance | 231 | 222 | 96.1% |
| features | 616 | 579 | 94.0% |
| regime | 166 | 157 | 94.6% |
| monitoring | 310 | 290 | 93.5% |
| risk | 501 | 456 | 91.0% |
| mt5_adapter | 565 | 501 | 88.7% |
| data | 1633 | 1394 | 85.4% |
| model | 1279 | 1063 | 83.1% |
| realtime | 1235 | 888 | 71.9% |
| execution | 3704 | 2576 | 69.5% |
| services | 291 | 202 | 69.4% |
| alerts | 1412 | 763 | 54.0% |
| logs | 246 | 89 | 36.2% |
| scripts | 9593 | 3223 | 33.6% |
| **TOTAL** | **22288** | **12889** | **57.8%** |

## Core production packages (>69%)

The trading core is well covered:

- **config** 95.9%, **contracts** 96.4%, **provenance** 96.1% — the contract
  and configuration layers are nearly fully exercised.
- **features / regime / monitoring / risk / mt5_adapter / data / model** —
  83–94%: signal generation, risk gates and ledgering are solid.
- **execution** 69.5% and **realtime** 71.9% — the execution/realtime paths
  are covered on the trading-critical modules (trade_group_executor 96.5%,
  reconciliation, broker_adapter); the gap is mostly `execution/mt5_trader.py`
  (33.7% — the legacy monolith) and dashboard-adjacent code.

## Modules below 50%

The under-50% mass is concentrated in **one-off operational scripts and
diagnostics** (`scripts/diag_*`, `layer0/layer1_validation`, `telegram_admin`,
`watchdog`, `overnight`…) and two legacy interactive modules
(`alerts/challenge_commands.py`, `alerts/pair_monitor.py`). These are CLI
entry points that require live MT5/Telegram/process environments; per the TZ,
testing them is out of scope for this task. The full list is in
[COVERAGE_GAPS.md](COVERAGE_GAPS.md) as the backlog plan.

## Artifacts

- `htmlcov/index.html` — interactive per-line report (git-ignored).
- `coverage.json` — machine-readable summary (git-ignored).
- Re-run locally: `pytest -q --cov=. --cov-report=html`.

## Next steps (plan, not part of this task)

1. Priority: raise `execution/mt5_trader.py` toward the `execution/` package
   average — it is the largest single under-covered trading file (1042 stmts,
   33.7%).
2. `alerts/control_bot.py` (416 stmts, 46.4%) — extract testable pure helpers
   from the Telegram command surface.
3. `scripts/` — decide per-script: either promote to tested modules or mark
   as operational-only and exclude from the measured source set.
4. Medium-term target: ≥70% total by fixing the top five modules in
   [COVERAGE_GAPS.md](COVERAGE_GAPS.md).
