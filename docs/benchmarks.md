# Strategy Benchmarks & Honest Validation

This document records performance baselines and metrics for the `xauusd-alert-system`
walk-forward backtests and tracks model-improvement changes (Part B, Phases 0-6).

> **Rule**: every model/validation change MUST update these tables and MUST be
> accompanied by a green `pytest` run. Metrics must NEVER be recorded from an
> in-sample or leakage-contaminated evaluation.

---

## 1. Metric Schema

`backtest/metrics.py::compute_metrics` returns exactly these keys per fold:

| Key | Meaning |
|-----|---------|
| `n_trades` | Number of closed trades in the sample |
| `win_rate` | % of trades with pnl > 0 |
| `profit_factor` | Gross profit / |gross loss| (999.0 if no losses) |
| `sharpe_ratio` | Mean/std of per-trade pnl, annualized (sqrt(250)) |
| `sortino_ratio` | Mean/downside-std of per-trade pnl, annualized |
| `expectancy` | Mean pnl per trade ($) |
| `max_drawdown` | Min running drawdown of cumulative pnl ($, <= 0) |
| `total_pnl` | Cumulative pnl at end of sample ($) |
| `max_consecutive_losses` | Longest run of non-positive trade pnls |

Per-session breakdowns are produced by `backtest/metrics.py::compute_metrics_per_session`.

Walk-forward folds (`backtest/walk_forward.py`) are **strictly time-ordered and
non-overlapping in test**: `train_end_ts <= test_start_ts`, so no test-window
information can leak backward into training. Aggregating per-asset results happens by
averaging the per-fold metric rows saved to `logs/backtest_<asset>.csv`.

---

## 2. Honest Validation Protocol (Part B, Phase 0+1)

Two leakage sources were removed so numbers in this file are trustworthy:

1. **Purged, time-ordered model calibration** — `model/trainer.py::calibrate_model`
   no longer uses `CalibratedClassifierCV(cv=3)` (sklearn's internal **shuffled**
   K-fold). Instead:
   - The base model is fit **only** on an *earlier* slice of `X_train`.
   - A **purge gap** of `labeling.horizon_candles_n` rows (36 candles) is dropped so
     no label window can cross the fit/calibrate boundary.
   - The calibrator (`cv` = a single explicit time-ordered split, never a shuffle)
     is fit **only** on the strictly *later* trailing slice.
   - If the training set is too small for a valid purged split, `calibrate_model`
     degrades to identity (raw) probabilities — **never** a shuffled K-fold — and
     marks the model with `_is_honest_placeholder = True`.

2. **Walk-forward does not touch the production model** — `scripts/run_backtest.py`
   trains per-fold models in disposable temp files only (HIGH 11), so a backtest can
   never overwrite the deployed live-trader model.

This mirrors production semantics: *fit on old data -> calibrate on the most recent
data -> evaluate on the future (out-of-sample) window*, with no temporal overlap at
any boundary.

### How to reproduce a benchmark

```powershell
python -m pytest -q -p no:cacheprovider              # must be green before/after
python scripts/run_backtest.py --asset XAUUSD --timeframe M5 --db-path data/market_data_mt5.sqlite
python scripts/run_backtest.py --asset XAGUSD --timeframe M5 --db-path data/market_data_mt5.sqlite
python scripts/run_backtest.py --asset BTCUSD --timeframe M5 --db-path data/market_data_mt5.sqlite
python scripts/run_backtest.py --asset EURUSD --timeframe M5 --db-path data/market_data_mt5.sqlite
python scripts/run_backtest.py --asset GBPUSD --timeframe M5 --db-path data/market_data_mt5.sqlite
```

Fold-level metrics are written to `logs/backtest_<asset>.csv`. The **baseline** rows
below are recorded from the same commands AFTER the honest-calibration fix AND the two
money-unit fixes were applied (per-asset `slippage_usd` + per-asset `point_value_lot`,
see Change Log). These are the reproducible Phase-0+1 baseline; the pre-fix `cv=3`
numbers were leaked and are deliberately not reported, and the pre-unit-fix EURUSD/GBPUSD
(0% win-rate, ~−0.07 PnL pinning) and BTCUSD (+$377k, 100× inflation) numbers were
unit-broken and are deliberately not reported either.

---

## 3. Phase 0 Baseline (post honest-calibration fix)

Config snapshot: `model.type=xgboost`, `model.calibration_method=sigmoid`,
`labeling.method=atr_scaled`, `backtest.walk_forward` = 300d train / 50d test / 50d step.

Rows are per-fold aggregates across the 42 walk-forward folds from the commands above
(300d train / 50d test / 50d step). A fold can be empty when its test window lies
outside the exchange's trading hours for that asset (hence `min n_trades = 0` and
`win_rate 0.00`).

### XAUUSD (M5)

| Metric | n_trades | win_rate | profit_factor | sharpe | sortino | expectancy | max_dd | total_pnl | max_consec_loss |
|--------|---------:|---------:|--------------:|-------:|--------:|-----------:|-------:|----------:|----------------:|
| **avg (42 folds, 12,888 trades)** | 307 | 47.43 | 0.94 | -0.70 | -1.72 | -0.01 | -77.15 | -4.96 | 8.33 |
| **min** | 0 | 34.59 | 0.46 | -5.32 | -11.20 | -0.51 | -415 | -142 | 0 |
| **max** | 473 | 61.11 | 1.66 | 3.59 | 11.70 | 1.55 | 0 | 453 | 19 |

Sum `total_pnl` across folds = **−208.27**; positive-PnL folds = **17/42**.

### XAGUSD (M5)

| Metric | n_trades | win_rate | profit_factor | sharpe | sortino | expectancy | max_dd | total_pnl | max_consec_loss |
|--------|---------:|---------:|--------------:|-------:|--------:|-----------:|-------:|----------:|----------------:|
| **avg (42 folds, 16,228 trades)** | 386 | 23.27 | 0.38 | -8.69 | -22.28 | -0.95 | -404 | -385 | 22.71 |
| **min** | 0 | 9.15 | 0.04 | -32.72 | -126 | -1.40 | -778 | -780 | 0 |
| **max** | 623 | 50.18 | 1.08 | 0.54 | 1.56 | 0.58 | 0 | 160 | 72 |

Sum `total_pnl` across folds = **−16,190.32**; positive-PnL folds = **1/42**.

### BTCUSD (M5)

| Metric | n_trades | win_rate | profit_factor | sharpe | sortino | expectancy | max_dd | total_pnl | max_consec_loss |
|--------|---------:|---------:|--------------:|-------:|--------:|-----------:|-------:|----------:|----------------:|
| **avg (42 folds, 29,487 trades)** | 702 | 51.35 | 1.04 | 0.02 | 0.30 | 0.06 | -46.11 | 41.28 | 8.60 |
| **min** | 92 | 30.43 | 0.43 | -5.19 | -10.49 | -0.33 | -257 | -254 | 4 |
| **max** | 1,034 | 60.62 | 1.48 | 2.62 | 6.77 | 0.35 | -7.19 | 240 | 15 |

Sum `total_pnl` across folds = **+1,733.88**; positive-PnL folds = **25/42**.
Recorded AFTER the `point_value_lot: 1` fix (money scale now honest, see Change Log).

### EURUSD (M5)

| Metric | n_trades | win_rate | profit_factor | sharpe | sortino | expectancy | max_dd | total_pnl | max_consec_loss |
|--------|---------:|---------:|--------------:|-------:|--------:|-----------:|-------:|----------:|----------------:|
| **avg (42 folds, 12,657 trades)** | 301 | 25.25 | 0.32 | -12.02 | -26.82 | -0.22 | -74.08 | -73.60 | 16.40 |
| **min** | 0 | 0.00 | 0.00 | -131 | -131 | -0.31 | -153 | -153 | 0 |
| **max** | 613 | 37.07 | 0.55 | -4.08 | -10.29 | 0.00 | 0 | 0 | 39 |

Sum `total_pnl` across folds = **−3,091.11**; positive-PnL folds = **0/42**.

### GBPUSD (M5)

| Metric | n_trades | win_rate | profit_factor | sharpe | sortino | expectancy | max_dd | total_pnl | max_consec_loss |
|--------|---------:|---------:|--------------:|-------:|--------:|-----------:|-------:|----------:|----------------:|
| **avg (42 folds, 13,051 trades)** | 311 | 29.28 | 0.38 | -8.65 | -21.03 | -0.25 | -82.84 | -81.86 | 14.17 |
| **min** | 0 | 0.00 | 0.00 | -82.40 | -82.40 | -0.46 | -160 | -160 | 0 |
| **max** | 573 | 50.00 | 0.69 | 0.00 | 0.00 | 0.00 | 0 | 0 | 34 |

Sum `total_pnl` across folds = **−3,438.10**; positive-PnL folds = **0/42**.

> **Recording note**: these are the first real `run_backtest.py` runs over
> `data/market_data_mt5.sqlite` (42 folds per asset). All rows are recorded AFTER the
> honest-calibration fix AND the two money-unit fixes (per-asset `slippage_usd` and
> per-asset `point_value_lot`, see Change Log), so PnL is in correct USD per lot per
> price unit. Any later Phase must record against the same commands and update these
> tables (the per-fold `avg/min/max` rows and the `positive-PnL folds` count).

> ⚠️ **2026-08-06 — baselines were recorded on the OLD grid.** The Phase-0 tables
> below predate the equal-step signal grid. Since then `model/ensemble_backtest.py`,
> `backtest/engine.py` and the EV gate all read `signal_grid:` (TP1/2/3 = 1/2/3×step,
> stop = 3×step, scale-out 50/30/20 + breakeven), while the old tables simulated
> TP 1.0/1.8/2.8 with stop 1.0. **Re-record the baselines before drawing conclusions**
> (same commands as above). Expect materially different (likely lower-trade-count)
> numbers under the wider stop.

---

## 4. Change Log

| Date (UTC) | Phase | Change | Impact | Test status |
|------------|-------|--------|--------|-------------|
| 2026-08-03 | 0+1 | Replaced `CalibratedClassifierCV(cv=3)` (shuffled K-fold, label-horizon leakage) with a purged, time-ordered calibration split (`model/trainer.py::calibrate_model`); added `_is_honest_placeholder` degraded path + 2 unit tests locking in no-shuffle / purge-gap => horizon. | Correctness: removes temporal leakage from calibration; out-of-sample metrics now honest. | `110 passed` (was 108; +2 new tests, 0 regressions) |
| 2026-08-03 | 0+1 | Added per-asset `slippage_usd` to `config/config.yaml` (XAGUSD 0.02, EURUSD 0.0002, GBPUSD 0.0002) so FX/XAG are charged real USD slippage instead of the global gold-default `0.05` points (which insta-stopped low-priced FX). | Correctness: fixes the FX 0%-win-rate bug — EURUSD corrected run went from ~0% WR / all trades pinned at ≈ −0.07 to an honest 25.25% mean WR / −0.223 expectancy; 3 regression tests added. | `116 passed` (0 regressions) |
| 2026-08-03 | 0+1 | Added per-asset `point_value_lot` (USD notional per 1.0 lot per 1.0 price unit) to `config/config.yaml`: XAGUSD 5000, EURUSD/GBPUSD 100000, BTCUSD 1 (gold default 100 kept for XAUUSD). Fixes the money-scale bug where the gold-default multiplier 100 swamped FX/XAG PnL with commission **and** inflated BTCUSD money-PnL ~100× (a fake +$377k baseline). Matches standard MT5 contract sizes (XAUUSD 100 oz, XAGUSD 5000 oz, FX 100k base, BTC 1). | Correctness: XAGUSD now −0.953 expectancy / −16,190.32 (realistic, was pinned); BTCUSD now +0.065 expectancy / +1,733.88 / 25/42 positive folds (was +13.42 / +$377k / 33/42 inflated); 2 regression tests added incl. the config-anchor contract-size test. | `117 passed` (was 116; +1 new test, 0 regressions) |
| 2026-08-03 | 2 | Phase-2 EV entry gate: added `ensemble.ev_threshold` (default 0 = disabled) to `config/config.yaml` + `model/ensemble.py::compute_ensemble_signal`. When > 0, a candidate long/short is only emitted if `(reward*p − risk*(1−p))/risk > threshold`; otherwise it is declined to no_trade. | New capability (off by default): model-based EV trading, gated so the Phase-0+1 baseline is unchanged unless explicitly enabled. 4 regression tests added (disabled-by-default, breach-→-no-trade, positive-EV-→-trade, payoff-ratio). | `117 passed` (+4 new tests, 0 regressions) |
| 2026-08-03 | 2 | Phase-2 3-class model: added `model.include_zero_class` (default false = binary) to `config/config.yaml`; `model/trainer.py` now keeps label 0 as its own class in 3-class mode and `model/predictor.py` exports `p_no_trade` alongside `p_long`/`p_short`. | New capability (off by default): a full long/short/hold model; binary baseline preserved unless explicitly enabled. 2 regression tests added (keeps-zero-label, 3-class end-to-end). | `123 passed` (+2 new tests, 0 regressions) |
| 2026-08-03 | 3 | Phase-3 regime-as-categorical-feature: added `model.use_regime_feature` (default false) to `config/config.yaml`; `model/trainer.py::build_training_matrix` expands the raw causal `regime` column into `regime_<label>` one-hots; `model/predictor.py` auto-synthesizes them from a raw `regime` column at inference time so `predict_proba`/`predict_single` work for run_backtest, diag_fx_slippage and the realtime pipeline unchanged. | New capability (off by default): the model can learn regime-conditional behavior directly. 4 regression tests added (gates, one-hot emission, match-raw, train+auto-synth end-to-end). | `127 passed` (+4 new tests, 0 regressions) |
| 2026-08-03 | 3 | Step 5d #26/#27: hardened `scripts/retrain_with_real_trades.py` with the documented merge contract and honest exit codes. #26 `prepare_real_trades_df` now always returns (X_real, y_real) with the full `feature_cols` (missing features default to 0.0) and drops malformed rows (empty/missing features, bad bias/outcome) without ever crashing; retraining always merges real wins/losses into the training set in binary mode. #27 `main()` now returns a per-asset stats list and an honest exit code (0 = all OK; 1 = any asset hard-failed, or the real-trade merge was skipped for every enabled asset because of `include_zero_class` / `use_regime_feature`) — the script still trains & saves the historical model but reports the missing real-trade payload so `scripts/overnight.py` stage 4 surfaces it (exit-1 → Telegram ❌) instead of a silent green tick. | Correctness/ops: a night whose models retrain on history but fold in no real trades (or partially fail) now FAILS stage 4 and notifies instead of silently succeeding; the merge contract is deterministic and never poisoned by a malformed trade row. 8 regression tests added (merge mapping + missing-feature fill, malformed-row drop, empty contract, 3-class/regime skip → not-ok, main exit codes OK / skip-all / hard-fail). | `135 passed` (was 127; +8 new tests, 0 regressions) |
| 2026-08-04 | 4-5 | Step 6 (#30/#41/#16): Phase 4-5 ensemble hardening in `model/ensemble.py::compute_ensemble_signal`, all behind config flags defaulting off in `config/config.yaml`. #16 `normalize_probs` re-scales `ml_p_long`/`ml_p_short` to sum exactly to 1.0 before any downstream use (degenerate/non-finite total → neutral 0.5/0.5, which never passes any filter). #30 `dynamic_min_confidence` makes the alert bar = base × `dynamic_min_confidence_scale` × (1 − min(`dynamic_edge_credit`, |p_long−p_short| × `dynamic_edge_gain`)) for per-asset tightening and edge-driven relaxation (complements, without conflicting with, the existing execution-time loss-streak bar `execution/mt5_trader.py::_get_dynamic_min_confidence`). #41 `hard_divergence_veto` forces no_trade when a non-zero rule vote opposes a non-zero ML vote (a tie `ml_vote == 0` is NOT a veto). | New capabilities (off by default): normalized directional probabilities (#16), per-asset dynamic entry bar (#30), hard rule/ML conflict resolution (#41); Phase-0+1..3 baselines unchanged unless enabled. 9 regression tests added (normalize off/on/degenerate; dynamic off/relax/tighten; veto off/on/tie). | `144 passed` (was 135; +9 new tests, 0 regressions) |
| 2026-08-04 | 6 | Step 7 (#25): deploy guard (degrade guard) in the nightly pipeline. Added `scripts/deploy_guard.py` (backup/check/status CLI) + `deploy_guard:` config block in `config/config.yaml` (`primary_metric: expectancy`, `fallback_metrics`, `min_trades: 20`, `tolerance: 0.0`, `backup_suffix`), wired into `scripts/overnight.py` as Stage 3b (`--backup` snapshots each enabled asset's production model to `<model_path>.deploy_guard.bak` BEFORE retrain) and Stage 4b (`--check` walk-forward validates the freshly retrained model vs the backed-up incumbent on the SAME freshly backfilled OOS windows; a regression beyond `tolerance`, or thin candidate evidence `< min_trades`, restores the incumbent and exits 1 → stage FAILED → Telegram ❌; can be skipped with `OVERNIGHT_NO_DEPLOY_GUARD=1`). No-look-ahead contract: the incumbent is a static file scored only on windows it was not retrained on that night; the candidate is trained per fold on that fold's train window ONLY (temp-file models, HIGH 11) and scored on the immediately following OOS window — identical windows on both sides. | Correctness/ops: the nightly retrain can no longer silently replace a good production model with a bad one; a rejected/errored night restores the incumbent and surfaces as exit 1 (`deploy_guard_check` FAILED) instead of a green tick. Conservative by default (`tolerance: 0.0`, `min_trades: 20`). 29 regression tests added (improvement/fallback/thin-evidence rules, fold aggregation, decision wrapper, backup idempotency, rollback-on-reject/error + backup cleanup, main exit codes). | `173 passed` (was 144; +29 new tests, 0 regressions) |
| 2026-08-06 | sim | Fixed three virtual-market integration bugs that blocked end-to-end simulation runs: (1) `scripts/run_simulation.py`/`run_bot.py` injected the MT5 shim via `from simulation.mt5_shim import MetaTrader5` — a SECOND module object — while the protected modules use `import MetaTrader5`, so `_inject()` was invisible and `run_loop` never saw new bars (no trades at all); now both scripts import the shim under its plain top-level name. (2) `VirtualState` registered symbols under asset keys (XAUUSD) while the trader validates by `mt5_symbol` (GOLD) — added `build_virtual_cfg()` which extends `symbol_overrides` with the main config's MT5 symbol names. (3) Circuit-breaker anchor never rolled ("rolling anchor" docstring vs static anchor): a 5% multi-bar drift froze the market permanently; the anchor now re-anchors on every closed M5 bar (single-bar shock protection retained). Plus `SignalResponse` now carries `step` so the API exposes the equal-step grid. 3 regression tests added. | `229 passed` (was 226; +3, 0 regressions) |
| 2026-08-06 | grid | Aligned every backtest/execution surface to the equal-step `signal_grid` (TP1/2/3 = 1/2/3×step, stop = 3×step): `backtest/engine.py` ATR-scaled barriers now come from `get_signal_grid` (was labeling target 1.2 / stop 1.0); EV gate in `model/ensemble.py` now computes payoff as `tp3_mult/stop_mult` (was `tp1_mult/stop_mult` — under the new grid 1/3, which would reject every trade when enabled; now 3/3 = 1.0, i.e. a pure probability-quality filter). Per-asset `signal_grid` overrides flow into `EnsembleBacktester` via `get_signal_grid(cfg, asset_cfg)`. | Correctness: backtest exits now mirror the live MT5/Telegram grid (multi-TP 50/30/20 + breakeven), so recorded PnL is comparable to real execution. Baselines from the OLD grid must be re-recorded (note added above). 2 regression tests added (grid barrier ratio 3:1 in the engine; EV gate TP3-payoff behavior incl. shipped 1:1 grid). | `231 passed` (was 229; +2, 0 regressions) |
| 2026-08-06 | 17 | Phase 17: order-flow features wired into training AND inference (`features/order_flow.py` was built and tested but never used): `cvd`, `cvd_slope_10`, `order_flow_imbalance_14/50`, `dist_vwap_atr` added to `FEATURE_COLUMNS` (41 -> 46) and to `build_full_df` in `scripts/train_mt5.py` / `scripts/run_backtest.py` and `realtime/pipeline.py::_build_features`, so training/inference stay consistent. SMC report metrics (manipulation index etc.) deliberately NOT vectorized into ML features — they are whole-frame report scalars for the dashboard, not per-row causal series. Plus per-asset `timeframe` overrides (`assets.<key>.timeframe`): XAGUSD/EURUSD/GBPUSD moved to M15 (M5 round-trip cost ≈ 25–100% of the TP1 step; M15 widens the grid ~2.5x), XAUUSD/BTCUSD stay M5; honored by `train_all_assets`, `run_backtest`, `overnight` (backfill runs every trade timeframe), `retrain_with_real_trades`, `deploy_guard`, `seed_db`, `RealtimePipeline`. | Capability: FX/XAG now trade a grid wide enough to absorb spread+slippage; models get microstructure signal. Baselines must be re-recorded per asset on its own timeframe. 4 regression tests added (order-flow columns in pipeline, in FEATURE_COLUMNS, per-asset timeframe resolution, M15 signal contract). | `235 passed` (was 231; +4, 0 regressions) |
