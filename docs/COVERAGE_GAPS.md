# Coverage Gaps — Backlog Plan

**Date:** 2026-08-28 · **Baseline total:** 57.8% · **Target:** ≥70%

Since coverage is below 70%, per the TZ this document records the
low-coverage modules (measured ≥10 statements, <50% lines) as a **plan**, not
as tests written within the same task. Ordered by expected risk-reduction per
unit of effort.

## Priority 1 — trading-critical

| Module | Statements | Coverage | Plan |
|---|---:|---:|---|
| `execution/mt5_trader.py` | 1042 | 33.7% | Largest under-covered trading file. Extract testable units (symbol/digits adjuster, stop-level math, order-send wrapper) into `execution/` modules with unit tests; keep the legacy file as a thin orchestrator. |
| `execution/fx_execution_probe.py` | 159 | 0.0% | Live FX probe; wrap the measurement logic in a pure function and add a mocked-API test, or mark operational-only and exclude from measurement. |

## Priority 2 — user-facing surfaces

| Module | Statements | Coverage | Plan |
|---|---:|---:|---|
| `alerts/control_bot.py` | 416 | 46.4% | Telegram command surface. Extract pure command handlers (parse → response text) and test them without the bot I/O layer. |
| `alerts/pair_monitor.py` | 206 | 13.6% | Split the scanning loop from the alert formatting; test the formatter against fixture pairs. |
| `alerts/challenge_commands.py` | 189 | 0.0% | Same pattern as `control_bot.py`. |
| `scripts/pairs_dashboard.py` | 105 | 14.3% | HTML assembly is string work — extract the renderer and snapshot-test. |
| `scripts/trial_window.py` | 386 | 11.1% | Long-running trial monitor; extract the state-machine transitions (start/pause/extend/finish) into a testable class. |

## Priority 3 — operational scripts (decide: test or exclude)

These are one-off diagnostics / CLI entry points requiring live environments
(MT5, Telegram, network, process control). For each, choose one of:

- **(A)** extract the core computation into a library module + unit test, or
- **(B)** declare operational-only and add to the `.coveragerc` omit list so
  the metric reflects the production surface.

| Module | Statements | Coverage |
|---|---:|---:|
| `scripts/layer0_validation.py` | 499 | 0.0% |
| `scripts/telegram_admin.py` | 427 | 0.0% |
| `scripts/overnight.py` | 185 | 0.0% |
| `scripts/diag_entry_vs_label.py` | 203 | 0.0% |
| `scripts/diag_regime_comparison.py` | 171 | 0.0% |
| `scripts/news_feed_browser_worker.py` | 160 | 0.0% |
| `scripts/process_manager.py` | 143 | 0.0% |
| `scripts/diag_calib_blend_direction.py` | 146 | 0.0% |
| `scripts/news_feed_server.py` | 146 | 0.0% |
| `scripts/watchdog.py` | 121 | 0.0% |
| `scripts/diag_traded_event.py` | 124 | 0.0% |
| `scripts/diag_trade_quality.py` | 128 | 18.8% |
| `scripts/diag_calib_ab_btcusd.py` | 130 | 0.0% |
| `scripts/layer1_validation.py` | 221 | 0.0% |
| `scripts/diag_direction_beta.py` | 117 | 24.8% |
| `scripts/diag_regime_quality_assets.py` | 107 | 0.0% |
| `scripts/grid_search_gbp.py` | 104 | 26.0% |
| `scripts/diag_gbp_profile.py` | 105 | 0.0% |
| `scripts/train_mt5.py` | 168 | 35.7% |
| `scripts/backfill_data.py` | 96 | 29.2% |
| `scripts/run_simulation.py` | 95 | 41.1% |
| `scripts/diag_bifurcation_dominance.py` | 98 | 0.0% |
| `scripts/run_bot.py` | 67 | 31.3% |
| `scripts/paper_accumulate_wide_filtered.py` | 88 | 46.6% |
| remaining `scripts/diag_*`, `scripts/calibration_portfolio_report.py`, `scripts/seed_db.py`, … | ≤100 each | <50% |

## Priority 4 — small utilities

| Module | Statements | Coverage | Plan |
|---|---:|---:|---|
| `logs/ws_live_test.py` | 90 | 0.0% | One-off websocket smoke — candidate for exclusion (B). |
| `logs/theme_retina_audit.py` | 41 | 0.0% | One-off UI audit — exclusion candidate. |
| `logs/mobile_audit.py` | 22 | 0.0% | One-off UI audit — exclusion candidate. |
| `mt5_adapter/lazy.py` | 13 | 46.2% | Small; add a direct unit test of the lazy import fallback. |

## Estimated impact

- Fixing Priority 1–2 alone (~2 500 uncovered statements weighted by reach)
  would move the total by roughly **+7–10 pp**.
- Applying option (B) to the operational-script block (Priority 3–4, ~3 500
  untested statements excluded from the denominator) would raise the reported
  total into the **mid-70s** even before new tests.
- Combination of both approaches reaches the ≥70% target with the least
  testing effort concentrated on real risk.
