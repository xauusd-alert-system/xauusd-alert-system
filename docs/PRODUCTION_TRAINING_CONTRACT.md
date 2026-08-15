# Production training contract (2026-08-15)

## Status

Implemented in code; economic acceptance still requires a pre-lock real-data A/B.
No locked/live-forward data may be used to choose the target or tune thresholds.

## Target resolution

`config/config.yaml` declares `labeling.contract_version: 2` and an explicit global
`labeling.event`. Per-asset `assets.<KEY>.labeling` overrides are merged by
`config.loader.effective_asset_config` before production feature generation and
labeling. `scripts/train_mt5.py` always passes `asset_key` to the label generator.

Current policy remains conservative:

- global default: `barrier` (legacy);
- XAUUSD: explicit `traded` opt-in already present in its asset config;
- no other asset is silently migrated.

Every new model bundle contains `metadata` with asset/timeframe, data period, target
and signal-grid contract, class counts, static cost assumptions, weighting and
calibration policy, and SHA-256 of the effective config. Old bundles remain loadable.

## Signal-grid causality and parity

The grid step is frozen from the **closed signal bar** (`step_points` or ATR,
then min/max clamps). TP/stop multipliers are applied exactly once. Traded labels,
`EnsembleBacktester`, the baseline engine, frozen paper, and live MT5 order setup
use this contract. The completed ATR of the next-open entry candle is unavailable at
its open and must not size the trade. This correction changes historical results;
all older baselines require revalidation.

## Production weighting

The default is `model.sample_weight_mode: uniqueness`. Weights are computed on the
full chronological feature frame and aligned by index to rows retained by the
training matrix. The same vector is used for the base estimator and the purged,
time-ordered calibration fit (`calibration_weight_mode: same_as_training`).

## Required legacy-vs-traded A/B

Use a **copy** of the real database and stop strictly before the configured locked
hold-out start (currently 2026-08-08):

```bash
python -m scripts.run_backtest --asset XAUUSD --timeframe M15 \
  --db-path /path/to/copied.sqlite --end-date 2026-08-08 --label-event barrier
python -m scripts.run_backtest --asset XAUUSD --timeframe M15 \
  --db-path /path/to/copied.sqlite --end-date 2026-08-08 --label-event traded
```

Explicit runs write separate `*_barrier.csv` and `*_traded.csv` outputs and journal
the event as part of trial identity. Do not use `--allow-locked` for this comparison.
Do not change the signal grid or admission thresholds based on one run.

For standalone candidate bundles (not deployment), `train_mt5` supports the same
explicit override; use different output paths:

```bash
python -m scripts.train_mt5 --symbol XAUUSD --timeframe M15 \
  --db-path /path/to/copied.sqlite --end-date 2026-08-08 --label-event barrier \
  --output output/models/ab/xauusd_barrier.joblib
python -m scripts.train_mt5 --symbol XAUUSD --timeframe M15 \
  --db-path /path/to/copied.sqlite --end-date 2026-08-08 --label-event traded \
  --output output/models/ab/xauusd_traded.joblib
```

## Empirical execution data

OHLCV tables now migrate in place to retain optional broker `spread` and
`real_volume`. Existing rows receive NULL and are not fabricated. Upserts from a
source without optional fields do not erase observations already stored.

`data.execution_ledger` adds an append-only `execution_fills` table with requested
and completed timestamps, requested/fill prices and volumes, adverse slippage,
latency, status/retcode/rejection reason, and order/position tickets. Live opens and
partial closes write best-effort telemetry. Generate a report with:

```bash
python -m scripts.execution_cost_report --db-path data/market_data_mt5.sqlite \
  --asset XAUUSD --timeframe M15 --output logs/xauusd_execution_costs.json
```

The traded-label bundle still identifies its cost source as `static_config`. Broker
observations must accumulate and pass data-quality review before empirical
percentiles replace configured assumptions.

## Frozen paper and one-time validation

See `docs/CANDIDATE_WIDE_TREND_FILTERED.md`. `scripts.paper_accumulate` freezes the
model/config/variant, records idempotent append-only events, and exposes only sample
counts during accumulation. `scripts.run_live_forward_validation` refuses to read
outcomes before the minimum and records an irreversible `validation_read` marker.

## Pre-lock MTF contribution

The controlled `[M15,H1]` versus `[H1,H4]` comparison is available without touching
the lock:

```bash
python -m scripts.compare_mtf_references --asset XAUUSD \
  --db-path /path/to/copied.sqlite --end-date 2026-08-08 \
  --output logs/mtf_reference_comparison.json
```

Higher-timeframe reads are capped at the base sample's last timestamp before feature
building. The script is research-only and never deploys a model.
