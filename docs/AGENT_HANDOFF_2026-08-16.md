# Agent handoff — xauusd-alert-system

**As of:** 2026-08-16  
**Working branch:** `arena/01a0068f-xauusd-alert-system`  
**HEAD at handoff:** `170b822`  
**Base audited originally:** `5253a7b`  
**Last full test:** `546 passed, 11 warnings`  
**Locked/live-forward outcomes read:** **NO**

## 1. Non-negotiable safety state

Current config is deliberately fail-closed:

```yaml
deployment.mode: research
retraining.enabled: false
retraining.schedule.enabled: false
execution.enabled_assets: []
execution.require_demo_account: true
```

An explicit empty execution allowlist means deny-all. Do not restore the old
BTCUSD allowlist: its admission gate was measured before the causal grid-parity
correction and is invalid under the current geometry.

Do not enable automatic retraining, paper accumulation, demo execution, or live
execution until the pre-lock real-data sequence in section 8 is complete and a
separate reviewed promotion commit exists.

## 2. Why old results are invalid

Commit `89350d5` unified trade geometry around a causal signal-bar step:

1. step is frozen from the closed signal bar;
2. fixed step/min/max clamps are applied;
3. TP/stop multipliers are applied once;
4. traded labels, both backtest engines, paper and live setup use this contract;
5. the completed entry-bar ATR is no longer used at next-open.

Consequently, old XAUUSD `wide_trend_filtered` figures and the old BTCUSD gate
are historical only. `docs/CANDIDATE_WIDE_TREND_FILTERED.md` is marked
`INVALIDATED BY GRID-PARITY CONTRACT; PAPER START BLOCKED`.

Do not cite old PF/PnL/DSR/null-control numbers as current evidence.

## 3. Main completed engineering work

### Target/training contract

- XAUUSD has explicit `labeling.event: traded`.
- `train_mt5.build_full_df` resolves per-asset config and passes `asset_key`.
- `train_mt5 --end-date` truncates raw data before feature building.
- Model schema-v2 metadata includes asset, event, geometry, class balance,
  effective config hash, strategy identity, data period and OOS calibration.
- Production and primary research paths use aligned uniqueness weights in base
  fit and calibration.
- `retraining.real_trade_merge_enabled: false`; selected executed trades are not
  silently mixed into unconditional direction labels.

### Costs and broker evidence

- Candle storage migrates `spread` and `real_volume` without erasing prior values.
- `execution_fills` records requested/fill price, volume, latency, slippage,
  retcode and rejection reason.
- `scripts.execution_cost_report` reports empirical distributions.
- Static configured costs remain the label fallback until a real sample exists.

### Frozen paper

Source of truth:

- `data/paper_ledger.py`
- `paper/accumulator.py`
- `scripts/paper_accumulate.py`
- `scripts/run_live_forward_validation.py`

The ledger is event-sourced, idempotent and protected from UPDATE/DELETE.
Manifest checks model SHA, schema-v2 metadata, target, timeframe, training-period
end and exact locked-start date. Validation requires the minimum, explicit
`--force`, and writes `validation_read` before reading outcomes. Concurrent
second readers lose the marker race and abort.

No manifest has been created and accumulation must remain blocked until a new
candidate is preregistered.

### Operational integrity

- Formal deployment modes live in `config/deployment.py`.
- Versioned strategy source is `config/strategy_spec.yaml`.
- Public contract is `contracts/signal_spec.py`.
- Signal lifecycle is `watch -> armed -> confirmed`, with reject/expire paths.
- Primary source of truth is hash-chained `data/trading_event_ledger.py`.
- Events link signal, publication, order, fill/rejection, stop changes, partials
  and broker close/PnL.
- Telegram publish latency is recorded.
- Dashboard Monte Carlo reads primary `position_closed` events, not samples.
- Telegram archive importer stores only unlinked descriptive records and never
  computes WinRate.

### Dashboard truthfulness

No fake live balance, static correlation, sample sentiment, hypothetical Monte
Carlo, random chart, hard-coded institutional metrics, fake empty portfolio, or
fake-success web closeall remains. Missing data is `available: false`.

Web controls require `DASHBOARD_CONTROL_TOKEN`; broker controls are intentionally
absent from the UI. Emergency execution control remains in authenticated Telegram.

### News, trailing and correlation

- Live news-feed failure is fail-closed.
- Optional historical calendar CSV is supported; without it, historical news is
  explicitly unmodelled.
- Live trailing uses causal pipeline ATR, never a price-percentage approximation.
- Correlation uses strategy-timeframe returns aligned by UTC, not positional M5 closes.

## 4. Selected strategy decision

The selected future policy is **fully systematic only after promotion**. The
contract includes `human_confirmed` as a fail-closed deployment mode, but no
Telegram `/confirm` execution workflow was built because that mode was not selected.

Current mode is `research`, therefore all broker routing is blocked.

## 5. Known limitations (do not misrepresent)

1. SignalSpec supports arbitrary target legs, but current execution/backtest policy
   remains three legs. Any TP4/nonstandard execution requires a new strategy
   version and engine validation.
2. Primary event ledger only covers events generated after deployment of this code;
   legacy `executed_trades` rows are not magically backfilled or linked.
3. No real historical news CSV is present.
4. No empirical broker sample large enough to replace static costs exists.
5. No real-data barrier-vs-traded or MTF comparison has run in this sandbox.
6. No current schema-v2 frozen candidate model/manifest exists.
7. Telegram HTML parsing is intentionally conservative and leaves all rows unlinked.
8. `hmm_classifier.py` is still a research-only GMM despite its legacy filename.
9. Fractional-diff/CUSUM/meta-model work remains deferred until a valid baseline.
10. `settings.py` mentioned in external notes is unrelated garbage and is absent here.

## 6. Files to read first

1. `docs/POST_PULL_RUNBOOK.md`
2. `docs/PRODUCTION_TRAINING_CONTRACT.md`
3. `docs/STRATEGY_SPEC.md`
4. `docs/CANDIDATE_WIDE_TREND_FILTERED.md`
5. `docs/DASHBOARD_DISCLOSURE.md`
6. `docs/COMPLIANCE_DISCLOSURE.md`
7. `docs/DEEPSEEK_V4_PRO_MAX_REAUDIT.md`
8. `config/strategy_spec.yaml`
9. `config/config.yaml`

## 7. Relevant commit history

```text
170b822 feat: add versioned strategy lifecycle and primary trading ledger
6b0822a chore: freeze retraining and execution pending geometry revalidation
89350d5 Reaudit target parity paper safety and dashboard data integrity
5253a7b test: update gate checks after IS->OOS informativeness rename
```

The Arena branch is session-fixed. Continue only on
`arena/01a0068f-xauusd-alert-system`.

## 8. Exact next work — requires owner's real DB

After pull and a green local test, use a copied database ending research at the
locked boundary. Never use `--allow-locked`.

```powershell
Copy-Item data/market_data_mt5.sqlite data/market_data_ab_20260815.sqlite

python -m scripts.run_backtest --asset XAUUSD --timeframe M15 `
  --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 `
  --label-event barrier

python -m scripts.run_backtest --asset XAUUSD --timeframe M15 `
  --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 `
  --label-event traded

python -m scripts.compare_mtf_references --asset XAUUSD `
  --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 `
  --output logs/mtf_reference_comparison.json

python -m scripts.deflated_sharpe --asset XAUUSD `
  --db-path data/market_data_ab_20260815.sqlite `
  --variants current,wide,wide_trend_filtered,null `
  --end-date 2026-08-08 --historical-trials 738
```

Train A/B artifacts with the same cutoff and separate paths. Record command,
commit, config hash, DB snapshot hash and outputs.

Then:

1. decide barrier vs traded;
2. decide MTF references;
3. rerun all required gates under the current geometry;
4. either invalidate the candidate or commit a new dated preregistration;
5. only then train/freeze schema-v2 candidate model and start paper accumulation.

## 9. What must not happen next

- Do not tune against locked/live-forward data.
- Do not run `run_live_forward_validation` early.
- Do not enable BTC/XAU execution from historical comments.
- Do not re-enable scheduled retraining before baseline selection.
- Do not add Transformer/Mamba/TimesFM/large neural ensemble.
- Do not present OHLCV order-flow proxies as L2 data.
- Do not infer performance from Telegram tags or unmatched channel messages.
- Do not combine a model/threshold change with a deployment promotion commit.

## 10. Verification at handoff

```text
pytest -q: 546 passed, 11 warnings
python compileall: clean
git diff --check: clean
dashboard JavaScript node --check: clean
```

Warnings are the known Starlette/httpx deprecation and deliberately small
synthetic CSCV fixtures. They are not new test failures.
