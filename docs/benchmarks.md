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

## 3b. FX v4 session — final per-asset configs (2022–2026 walk-forward)

Recorded from the FX v3/v4 sessions on the user's real DB
(`python -m scripts.run_backtest --asset <ASSET>`, honest per-fold XGBoost,
lot 0.01, real spread/slippage/commission per asset). These are the CURRENT
shipped configs in `config/config.yaml`.

| Asset | TF | Config | exp | PF (мед) | total PnL | WR | плюс. фолды |
|-------|----|--------|----:|---------:|----------:|----:|----:|
| XAUUSD | M5 | стандарт (1/2/3, SL 3) | +0.10 | ~1.07 | +90 | 47% | 19/41 |
| BTCUSD | M5 | стандарт | +0.09 | ~1.06 | +151 | 73% | 24/42 |
| XAGUSD | M15 | стандарт | +0.84 | 0.91 | +58 | 65% | 6/14 |
| EURUSD | H1 | v3: BE=0.5, stop=2.0, conf 0.85, h48 | +0.08 | 1.19 | +30 | 35% | 17/26 |
| GBPUSD | H1 | v4: stop=3.0, BE=1.0, tp2=2.5, tp3=3.0, conf 0.80, h36 + model flags | +0.12 | 2.42 | +138 | 36% | 17/24 |

Key facts behind these numbers (see `docs/GBP_FIX_STRATEGY.md`, `docs/FX_V3.md`):

- **Costs decide the timeframe.** M5 FX round-trip cost ≈ 98% of the TP1 step;
  M15/H1 widen the grid so costs eat far less of the target.
- **Early breakeven is per-asset.** `breakeven_trigger_atr: 0.5` helps
  mean-reverting EUR (PF 1.19), HURTS trending GBP (PF 0.84 → removed → 2.42).
  GBP gets room: wide stop (3.0), BE only at TP1, TP2 2.5 / TP3 3.0.
- **Entry-quality filters (conf 0.92 / EV gate / hard veto) gave no edge** and
  were reverted for FX (inherited global defaults).
- XAGUSD PF(мед) < 1 with total PnL > 0: 6/14 positive folds, median fold PF
  dragged by a few bad folds — the weakest asset; monitor after DSR/CSCV.

> ⚠️ These rows were recorded in the closed sandbox session; **re-run the same
> commands on the machine DB before acting on them** (configs are now synced to
> master, so numbers must match).

## 3c. Multiple-testing risk: Deflated Sharpe / CSCV (2026-08-07)

The grid-searches tried ~700 hyper-parameter combinations on the same
walk-forward folds — every headline number above is the BEST of many draws.
Selection bias is NOT captured by the metrics tables. The new tool:

    python -m scripts.deflated_sharpe --asset GBPUSD            # full family
    python -m scripts.deflated_sharpe --asset EURUSD --historical-trials 200
    python -m scripts.deflated_sharpe --asset XAUUSD --variants current,wide,null

What it computes (math in `backtest/deflated_sharpe.py`, tests in
`scripts/tests/test_deflated_sharpe.py`):

- **DSR** = probability that the config's true per-trade Sharpe > 0 after
  deflating by the expected max Sharpe under N trials (Bailey & López de Prado
  2014), with skew/kurtosis correction. `dsr_trials` uses only the family in
  the run; `dsr_historical` uses `--historical-trials` (default 729 = the
  project's total search history). Correlated trials make the full count
  conservative — the honest direction.
- **MinTRL** = trades needed before the edge can be told apart from the
  best-of-N null (also in years).
- **CSCV PBO** = probability the in-sample-best config is in the bottom half
  out-of-sample across all half-block splits of the fold-return matrix
  (Bailey et al. 2015). PBO ≤ 0.2–0.3 with positive mean λ is the healthy
  regime; PBO > 0.5 means the selection process is overfitting.
- A **`null` variant** (random 0.5±0.05 probabilities, same rules/grid/session
  filters) is always included as a negative control: it isolates the ML
  contribution from the grid+BE mechanics. If `current` and `null` both show
  DSR ≥ 0.95, the edge lives in the exit mechanics, not the model.

Output: `logs/deflated_sharpe_<asset>.csv` + `.json` (per-trial rows: SR,
PSR(0), DSR(n), DSR(729), MinTRL, PF, PnL, positive folds; CSCV summary).

> **Decision rule for each asset:** keep the config only if `dsr_historical ≥
> 0.95` (or at least clearly above `null`) AND `PBO ≤ 0.3`. XAGUSD (6/14
> positive folds) and the two FX pairs are the first candidates to test.

## 3d. Quant audit actions (2026-08-07)

A third-party quant audit of the system (`docs` / transfer notes, 2026-08-07)
re-prioritized the roadmap. Findings + actions taken this session:

1. **Labeling directional bias (FIXED).** A bar touching BOTH barriers in one
   candle was always labeled `-1` (short) in `labeling/label_generator.py` —
   a systematic skew of training labels toward the lower barrier. With
   OHLC-only data the intrabar order is unknowable, so the observation is now
   EXCLUDED (NaN → dropped by `build_training_matrix`), per the audit's rule
   «tick replay or exclude/zero-weight». `backtest/engine.py` already handled
   its own same-bar double touch conservatively (stop first). +3 tests.
2. **N_eff (IMPLEMENTED).** `effective_number_trials` in
   `backtest/deflated_sharpe.py`: `N_eff = 1 + (M−1)(1−ρ̄)` with ρ̄ = mean
   pairwise correlation of trial return streams + eigenvalue participation
   ratio. `scripts/deflated_sharpe.py` now reports DSR at **N_eff** (ρ̄ from
   the run's family, extrapolated to the historical trial count) AND at full
   N=729 as stress. With ρ̄=0.95 → N_eff(729)≈37; ρ̄=0.90 → ≈74 (audit
   examples). +3 tests.
3. **CSCV scorecard (IMPLEMENTED).** `cscv_pbo` now also reports **OOS
   probability of loss** of the IS-best config and its **IS→OOS relative
   degradation**. Both printed in the CLI report. +1 test.
4. **Exit-path contribution report (NEW TOOL).** `scripts/exit_profile.py`
   runs the honest walk-forward for an asset and reports PnL contribution by
   exit path (SL_pre_TP1 / BE_early / TP1_BE / TP1_SL / TP1_timeout /
   TP2_exit / TP2_trailing / TP3 / timeout) per asset × regime, in money AND
   net R (pnl ÷ money(|entry − initial_stop|)), plus the payoff geometry
   (avg win/loss in R, breakeven WR). `Trade` gained audit fields
   `tp1_hit`, `tp2_hit`, `initial_stop_price` (backward compatible).
   This is the audit's «первый отчёт» for the payoff-asymmetry diagnosis
   (WR 72–73% + PF≈1.06 ⇒ most wins end at TP1/BE, rare full SLs eat the
   profit). +5 tests.
5. **XAGUSD → shadow (DONE).** `assets.XAGUSD.enabled: false` — out of live
   trading/retrain/overnight (all prod paths honor `enabled`); research via
   explicit `--asset` scripts continues. Return gate per audit: ΔSharpe
   ≥ +0.10 vs the 4-asset portfolio on outer-OOS, or ES95/maxDD −10%.
6. **No new big grid / no LSTM (DECISION).** Audit: further TP/SL/BE grids
   worsen selection bias; deep models on the same 46 tabular features are
   unlikely to beat XGBoost. Next model work = meta-sizing (audit item 4)
   on OOF predictions only, with Brier/calibration/decile-uplift gates.

**Audit gates adopted for production:** DSR ≥ 0.95 (at defensible N_eff) AND
PBO ≤ 0.10 → capital; PBO 0.10–0.20 → shadow/reduced size; > 0.20 → selection
procedure considered overfit. Post-freeze shadow: 3–6 months of untouched
live-forward data (≥100–150 trades per strategy) before promotion.

## 3e. Second quant audit (Claude 5 Opus) — applied 2026-08-07

The two model answers were compared; Claude 5 Opus's plan was selected (more
operational: exact formulas, numeric gates, week-by-week work order). What was
implemented this session:

1. **R-multiplicator metrology** (`backtest/metrics.py`): `trades_to_dataframe`
   now carries `entry_price` / `initial_stop_price` / `tp1_price` / `volume`;
   `compute_r_metrics` gives E[R], σ[R], skew/kurtosis of R, payoff geometry
   (avg win/loss in R, breakeven WR) and the exit-bucket table (count, share,
   mean R, R contribution). `block_bootstrap_t` (block = holding horizon),
   `fold_sign_test` (exact binomial, one-sided), `summarize_folds` with the
   **arithmetic-consistency check** (audit 0.1: median PF > 1 requires ≥ 50%
   positive VALID folds; empty folds must not pollute one statistic only).
   `scripts/run_backtest.py` prints the fold summary + sign test + consistency
   warning on every run (also saved to `logs/backtest_<asset>_fold_summary.csv`).
2. **Detectability framing adopted**: with σ(R) ≈ 0.35–0.45 the grid's R cap
   (+0.567 / −1.0) means 600–1500 trades can only resolve E[R] ≥ 0.04–0.05
   (PF ≈ 1.20–1.30). XAU/BTC PF 1.06–1.07 are below the detection threshold —
   the goal is PF ≥ 1.2 or "unmeasurable", not more validation.
3. **Decision gate** (`scripts.deflated_sharpe.py::decision_gate`, printed in
   every report): block-bootstrap t ≥ 3.0, DSR(N_eff) > 0.95, PBO < 0.30,
   PF > 1.1 at 1.5× costs, positive folds ≥ 55% of valid, IS→OOS slope ≥ 0.5,
   locked hold-out (organizational). All conditions simultaneously, else
   paper/shadow. **Cost stress**: `run_analysis` reruns the current config at
   1.5× spread/slippage/commission and reports PF under stress.
4. **CSCV slope**: `cscv_pbo` now also returns `is_oos_slope` (pooled OOS-SR on
   IS-SR regression across splits; ≥ 0.5 = informative, ~0/negative = overfit).
5. **Week-1 measurements** (`scripts/diag_r_metrics.py`): per asset, honest
   walk-forward → R metrics + buckets, `cost_ratio` = (spread + 2·slippage +
   commission_in_price) / mean_step with the audit's zones (norm < 8–10%,
   red > 15%), events-per-feature (audit rule ≥ 10; EUR H1 ≈ 120 events / 46
   features ≈ 2.6 → over-parameterized), MFE/MAE in steps over the horizon per
   regime — P(MFE ≥ 1/2/3/5) and MAE p50/p80/p90 — as the calibration input for
   exit geometry (Action 4; no barrier tuned here, only measured), fold sign
   test. Outputs `logs/diag_r_metrics_<asset>.csv/.json`.
6. **Pre-registered TF hypotheses** (commented in `config/config.yaml` for
   XAUUSD/BTCUSD): M5 → M15 ONLY if the real-DB `cost_ratio > 15%` (measure
   with the diag script on both TFs first). Config not flipped without
   measurement, per the audit's own rule; XAG stays shadow.
7. **Decisions confirmed**: no new grids (each trial lowers DSR of everything
   found), no LSTM/Transformer on 46 tabular features (10²–10³ events per fold
   vs 10⁵+ needed), meta-labeling only as continuous sizing AFTER an asset
   passes the gate (target label «TP2 before SL», AUC ≥ 0.55 pre-check, meta
   features must be NEW information: strategy state, cross-asset z, IV state).

**Where improvement is unlikely (audit §3, adopted):** more features from the
same OHLCV; LSTM/Transformer; another TP/SL/BE grid; saving XAG; CVD from MT5
tick volume as alpha; COT as intraday entry; explicit vol targeting on top of
ATR stops; lot scaling as "making the system profitable".

## 3f. Week-1 measurements + exit geometry + org measures (2026-08-07, continued)

Continued the Claude plan past the metrology core:

1. **Look-ahead check at entry** (`scripts/diag_entry_timing.py`): runs the
   honest walk-forward under `next_open` (honest) vs `signal_close`
   (look-ahead measurement; `EnsembleBacktester.fill_mode`). Reports E[R]/PF/
   t_block per mode plus the close→next-open gap in ATR (the size of the
   look-ahead advantage). Verdict: honest fill must keep ≥ 70% of the
   look-ahead edge, else part of the result is look-ahead.
2. **Queue-loss measurement** (`scripts/diag_r_metrics.py` + engine
   `rejected_signals` / `simulate_blocked_entry`): signals rejected by the
   one-position-at-a-time constraint are simulated through the engine's own
   exit logic (forced entry at next open, `max_trades=1`). If E[R] of
   rejected ≥ E[R] of taken, the constraint itself destroys the edge → the
   audit's recommendation (2 half-size positions per asset once the
   portfolio layer exists) applies.
3. **Trial journal** (`scripts/trial_journal.py`): append-only
   `logs/trial_journal.csv`; `run_backtest`, every `grid_search_gbp` combo and
   `deflated_sharpe` log automatically. DSR's deflation N now defaults to the
   journal's real trial count (floor 729) — the project's true history
   includes the conf/EV/divergence/TF/BE experiments, not just the last grid.
4. **Locked hold-out guard**: `validation.locked_holdout` in config; every
   walk-forward runner refuses to run when its test windows overlap the
   reserved period unless `--allow-locked` (which burns the lock).
5. **Per-regime exit policy (engine + live)**: `signal_grid.regime_overrides`
   resolved by `get_signal_grid(cfg, asset_cfg, regime=...)` and honored by
   `EnsembleBacktester` (per-trade stop/TP/BE/trailing), the realtime
   pipeline (live targets → mt5_trader parity) and the EV gate. Per-regime
   scaleout ratios (e.g. 30/30/40 trend, 60/40/0 range) replace the hard
   50/30/20. Default config unchanged (bit-identical legacy behavior).
   Pre-registered (commented) trend/range policies in `config/config.yaml`
   for GBP.
6. **Exit-geometry calibration** (`scripts/exit_calibration.py`): SL/TP1/TP2
   and trailing-vs-TP3 from MFE/MAE per regime, calibrated on TRAIN windows
   only (never OOS). The grid-search alternative that does not burn trials.
   Values are proposals; applying them is a separate pre-registered step.

**Week-1 measurements produced on synthetic data** (numbers NOT real):
GBP cost_ratio 44% (RED zone — the audit's M5-cost concern), σ[R] 0.35,
skew(R) −1.37 (audit's negative-asymmetry geometry), queue loss shows
rejected E[R] ≥ taken E[R], honest fill keeps ~80% of the look-ahead edge.

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
| 2026-08-06 | FX var2 (tighten) | Ужесточение фильтров качества для убыточных FX (только per-asset): `assets.EURUSD.ensemble` и `assets.GBPUSD.ensemble` → `min_confidence_to_alert: 0.85 → 0.92`, `ev_threshold: 0 → 0.10`, `hard_divergence_veto: false → true` (глобальные `ensemble.ev_threshold: 0`, `hard_divergence_veto: false` без изменений; per-asset переопределение через `scripts/run_backtest.py::merge_asset_cfg` и `realtime/pipeline.py::effective_cfg`; `model/ensemble.py::compute_ensemble_signal` читает оба ключа из уже смерженного `ens_cfg` — проверено). XAUUSD/XAGUSD/BTCUSD и их per-asset `ensemble`/`signal_grid`/`labeling`/`timeframe` не трогались. | Ожидаемый эффект: сдвиг EUR/GBP с текущих `expectancy −0.26/−0.24`, `PF 0.65/0.73`, `0/14` и `2/14` плюсовых фолдов к `expectancy > 0`, `PF > 1`, `≥5/14` за счёт отсечения слабых (blended < 0.92), рассинхронизированных (hard veto) и низко-EV (<0.10) сигналов — меньше, но качественнее сделки. Риск: если walk-forward даст <~20 сделок/фолд — по ТЗ ослабить `min_confidence_to_alert` до 0.88–0.90 или добавить `normalize_probs: true` (каждое изменение — отдельный замер). Реальные цифры — после перезамера пользователем: `python -m scripts.run_backtest --asset EURUSD` / `GBPUSD` (БД у агента нет; таблицы базлайнов намеренно НЕ переписаны и будут перезаписаны пользователем). Критерий решения (по новым CSV): оставляем актив только если `expectancy > 0` и `PF > 1` и `плюсовых фолдов ≥5/14`, иначе `enabled: false` + правка `realtime/app.py` на фильтр по `enabled`. | `240 passed` (was 235; +5 new per-asset override tests, 0 regressions) |
| 2026-08-06 | FX v3 | **FX v3 (exit-mechanics package)**
| 2026-08-06 | FX v4 / GBP fix | **GBPUSD FX v4 «развернуть подход» (трендовый)**: Диагностика + grid-search с защитой от переобучения + трендовые конфиги (v4a/v4b/v4c) + per-asset  флаги. - : H1 + order-flow, , exit dist + "цена раннего БУ" + стоп-хант доля. - : 2-stage (coarse 27 + fine), критерии: median PF >1 + pos folds + ≥10 trades/fold + deferred last-6-folds ≥4/6. - v4a (config): stop=3.0, BE=1.0, tp2=2.5, tp3=4.0. - v4b (code):  (loader + ensemble_backtest + mt5_trader) — остаток 20% после TP2 трейлится. - v4c (config): H4 + horizon=24. - Per-asset model:  +  + retrain support. - GBPUSD config updated (комменты +  + примеры v4). - Новые тесты: trailing exit, per-asset model merge, smoke diag/grid. XAU/XAG/BTC/EUR не тронуты. Глобальные секции без изменений. | Диагностика + защищённый поиск трендовых конфигов для GBP. Реальные результаты — после прогона пользователя (, , ). См. . Критерий успеха: exp>0, med PF>1, ≥10/24 pos folds, ≥4/6 на 2024-26. |  |
: the var-2 entry filters (0.92 / EV 0.10 / hard veto) did NOT move the needle (EUR exp −0.26 / PF 0.65 / 0/14; GBP exp −0.24 / PF 0.73 / 2/14) — the problem is the EXIT, not the entry: at WR 62–66% a 1:3 grid (TP1 = 1×step, SL = 3×step) is mathematically negative before costs (loss tail −3×step ≈ 6× the average +0.5×step win). FX v3 attacks the tail, only for EURUSD/GBPUSD: per-asset `timeframe: H1` (was M15), `signal_grid.stop_mult: 2.0` (was 3.0), new `signal_grid.breakeven_trigger_atr: 0.5` (early BE: SL moves to entry once price covers 50% of the TP1 distance, BEFORE TP1), `labeling.horizon_candles_n: 48` (was 36), per-asset `ensemble.min_confidence_to_alert: 0.85` WITHOUT `ev_threshold`/`hard_divergence_veto` (var-2 keys removed → inherit global 0/false). Implemented in all three engines: `config/loader.py::get_signal_grid` (new normalized key `breakeven_trigger_atr`, default 1.0), `model/ensemble_backtest.py` (new exit_reason `"breakeven"` when the early trigger fired; default 1.0 = bit-for-bit legacy, so XAU/BTC/XAG unchanged), `backtest/engine.py` (early BE moves the stop; exit labels stay "stop"/"target"/"timeout"), `execution/mt5_trader.py::check_and_move_breakeven` (live per-symbol SL-to-entry via `be_trigger_by_symbol`, only when `be_trigger < 1.0`). 4 new regression tests (ensemble-backtest scratch vs full-stop, engine early-BE limits loss, grid loader default+overrides incl. 0.0, engine 3:1 barrier test now skips stop-dist==0 trades). | Fixes the FX exit mechanics: most would-be −3×step losers become ~0 scratches; real numbers await the user re-measure (`python -m scripts.run_backtest --asset EURUSD/GBPUSD`, see `docs/FX_V3.md`). | `244 passed` (was 240; +4 new tests, 0 regressions) |
| 2026-08-07 | sync | **Master synced to the user-machine final state**: (1) `assets.GBPUSD` = FX v4 winner (stop 3.0, BE 1.0, tp2 2.5, tp3 3.0, conf 0.80, h36, model flags on) — replaces the v3 early-BE config that CUT GBP recoveries; (2) `scripts/run_backtest.py::strategy_fn_factory` now merges the per-asset `model` section (use_regime_feature / include_zero_class) so the GBP walk-forward trains the same 3-class + regime-feature model the live trader uses (was silently training the global binary model); (3) `scripts/diag_gbp_profile.py` real-data path scores the frame with the PRODUCTION model (was 0.5-default → zero trades → useless diagnostics); (4) `scripts/grid_search_gbp.py` — the leaky `_inject_ml_probs` (close.shift(-6) future bias → PF 6–30 / 27/27) is DELETED; every candidate is now scored by `run_backtest.strategy_fn_factory` (per-fold XGBoost, temp-file models, no look-ahead) as documented in the transfer notes; synthetic builder gained a `regime` column for the honest path; (5) v4b trailing test probe fixed (was touching the BE/trail stop on flat lows — impossible scenario). | Honesty + parity with the machine state: grid-search and backtest numbers now come from the same no-look-ahead machinery; diagnostics profile real model entries. Tests updated accordingly. | `250 passed` (0 regressions, 1 broken trailing test fixed) |
| 2026-08-07 | mult | **Multiple-testing assessment tool (priority #1 of the transfer notes)**: new `backtest/deflated_sharpe.py` (PSR / DSR / E[max SR_N] / MinTRL per Bailey & López de Prado 2014; CSCV PBO per Bailey et al. 2015, full-enumeration with seeded sampling cap) + `scripts/deflated_sharpe.py` CLI: runs a per-asset config FAMILY (GBP: current-v4 / v3_early_be / v4a / v4b_trailing / legacy / null) through the honest walk-forward (same per-fold models for every variant), reports SR, PSR(0), DSR(n), DSR(729), MinTRL, PF, PnL, positive folds + CSCV PBO; `null` = random-prob negative control isolating the ML edge from grid+BE mechanics; synthetic no-DB fallback for tests. Outputs `logs/deflated_sharpe_<asset>.csv/.json`. | Answers «~700 grid combinations — is the winner real?» for every asset (esp. GBP v4 / EUR v3 / XAG). Run on the machine DB: `python -m scripts.deflated_sharpe --asset GBPUSD` (decision rule: dsr_historical ≥ 0.95 AND PBO ≤ 0.3). | `270 passed` (+20: 16 math/unit + 4 script integration) |
| 2026-08-07 | fix | **Timestamp unit bug (pandas 3.x)**: `series.astype("int64") // 10**9` silently returns MILLISECONDS when the datetime resolution is µs (pandas 3.0 stores non-nano), so `data/ingestion.py::fetch_candles` (API backfill), `realtime/pipeline.py` (MT5 live frame) and every synthetic builder wrote timestamps ~1000× too small — new backfills would produce 0 walk-forward folds and mixed-unit DBs. Added resolution-independent `data/ingestion.to_epoch_seconds()` (timedelta arithmetic) and replaced all 8 call sites (ingestion, pipeline, grid_search, diag, deflated_sharpe, 3 test builders). | Correctness/ops: new backfills and live frames keep true epoch-seconds; regression test locks the behaviour and asserts the legacy idiom is still broken on this pandas (so a future resolution change cannot silently pass). | `271 passed` (+1 regression) |
| 2026-08-07 | audit | **Quant-audit findings implemented** (3rd-party audit, transfer notes): (1) **labeling bias FIXED** — a same-candle touch of both barriers was hard-coded `-1` (short) in both label generators; now excluded as NaN (OHLC cannot order intrabar touches; audit rule: tick replay or exclude). (2) **N_eff added** (`effective_number_trials`: N_eff = 1+(M−1)(1−ρ̄) + participation ratio); `scripts/deflated_sharpe.py` reports DSR at N_eff AND full N=729, CLI prints ρ̄/N_eff. (3) **CSCV scorecard extended** with OOS probability of loss and IS→OOS degradation of the IS-best config. (4) **New `scripts/exit_profile.py`**: exit-path contribution per asset×regime in money + net R, payoff geometry (avg win/loss R, breakeven WR); `Trade` gained `tp1_hit`/`tp2_hit`/`initial_stop_price` audit fields. (5) **XAGUSD → shadow** (`enabled: false`; all prod paths honor it; research scripts take `--asset` explicitly). (6) Decisions: no new TP/SL/BE grids, no LSTM/Transformer on 46 tabular features; next = meta-sizing on OOF predictions. | Honesty/risk: removes a systematic short bias from training labels; selection-adjusted DSR at defensible N_eff; payoff asymmetry now visible per path; XAG capital parked until outer-OOS return gate. | `283 passed` (was 271; +12: 3 label-bias, 3 N_eff, 1 CSCV, 1 run-analysis, 5 exit-profile incl. main) |
| 2026-08-07 | audit2 | **Second quant audit (Claude 5 Opus plan) applied**: (1) R-multiplicator metrology in `backtest/metrics.py` — `trades_to_dataframe` extended (entry_price/initial_stop_price/tp1_price/volume), `compute_r_metrics` (E[R], σ[R], skew/kurt, payoff geometry, exit buckets in R), `block_bootstrap_t`, `fold_sign_test`, `summarize_folds` + arithmetic-consistency check (audit 0.1: median PF > 1 vs positive-VALID-folds). `run_backtest` prints/saves the fold summary on every run. (2) Decision gate in `scripts/deflated_sharpe.py` (t≥3.0 block-bootstrap, DSR(N_eff)>0.95, PBO<0.30, PF>1.1 at 1.5× costs, ≥55% positive valid folds, IS→OOS slope ≥0.5, locked hold-out) + cost-stress rerun at 1.5× for the current config. (3) CSCV `is_oos_slope` (pooled OOS-on-IS SR regression across splits). (4) Week-1 measurements `scripts/diag_r_metrics.py`: R metrics + buckets, `cost_ratio` (norm <8–10%, red >15%), events-per-feature (rule ≥10; H1 assets over-parameterized at 46 features), MFE/MAE per regime (P(MFE≥1/2/3/5), MAE p50/p80/p90) as exit-calibration input, fold sign test. (5) Pre-registered commented TF hypotheses XAU/BTC M5→M15 gated on real-DB cost_ratio>15% (config not flipped blindly). (6) Adopted audit §3: no new grids, no LSTM/Transformer, meta-sizing only after the gate with AUC≥0.55 pre-check. | Correctness/risk: cross-asset comparisons now in R (never raw money); a gate that currently fails every asset → paper/shadow until evidence; cost and MFE/MAE measurements replace grid-search guessing. | `300 passed` (was 283; +17: 7 R-metrics, 10 diag/gate/slope) |
| 2026-08-07 | audit3 | **Claude plan continued — week-1 measurements + org**: (1) `EnsembleBacktester.fill_mode` (`next_open` honest / `signal_close` look-ahead measurement) + `scripts/diag_entry_timing.py` (E[R]/PF/t_block per mode, close→next-open gap in ATR). (2) Queue loss: engine records `rejected_signals` and `simulate_blocked_entry` (forced entry at next open, max_trades=1, engine's own exit logic); `diag_r_metrics` reports E[R] rejected vs taken. (3) Per-regime exit policy: `signal_grid.regime_overrides` in `get_signal_grid(cfg, asset_cfg, regime=...)`; honored by EnsembleBacktester (per-trade stop/TP/BE/trailing/scaleout ratios), realtime pipeline (live targets → mt5_trader parity), EV gate; default config bit-identical. Pre-registered commented trend/range policies for GBP. (4) `scripts/trial_journal.py`: append-only journal wired into run_backtest/grid_search/deflated_sharpe; DSR N defaults to journal count (floor 729). (5) Locked hold-out guard (`validation.locked_holdout`) enforced by all walk-forward runners unless `--allow-locked`. (6) `scripts/exit_calibration.py`: SL/TP1/TP2 + trailing decision from MFE/MAE per regime, TRAIN-only (no OOS touch). | Correctness/org: quantifies the look-ahead rent and the queue-constraint cost; per-regime exits implement the audit's trend-wide/range-fast law in backtest AND live; DSR deflation now uses the real trial history; research can no longer accidentally look at the reserved period; exit geometry stops burning trials via grids. | `319 passed` (was 300; +19: 6 engine fill/regime/scaleout/rejection/sim, 1 loader regime override, 12 journal/holdout/calibration/timing) |
