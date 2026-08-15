# Candidate: wide_trend_filtered (XAUUSD)

Status: PRE-REGISTERED, PAPER/SHADOW ONLY
Date fixed: 2026-08-15

## Parameters

- Base variant: wide (stop_mult 4.0, BE trigger 1.0, TP3 4.0)
- Regime filter: trend_up ONLY
  (suppress compression, reversal_watch, range, trend_down)
- Sessions: london, newyork
- All other settings unchanged from config.yaml

## Pre-lock performance (2024-??..2026-08-08, 12 folds, --end-date 2026-08-08)

- n_trades: 339
- PnL: +4933.2
- PF: 1.54
- WR: 80.8%
- Cost x1.5 PF: 1.48
- t_block: 1.43
- DSR(N_eff): 0.73
- Fold health: 6/8 positive, ex-best +2518.6

## Null control in same segment (trend_up + london/newyork)

- n_trades: 262
- PF: 0.90
- R_mean: -0.036
- t_block: -0.996

Conclusion: the regime filter alone is unprofitable with random entries.
The ML signal plus wide exits carries the edge.

## Live-forward validation rule

- Data: from 2026-08-08 onward (locked hold-out).
- Trigger: when >= 50 trades accumulate for wide_trend_filtered.
- Method: one outcome read from the frozen append-only paper ledger.
- Success criteria:
  - PF >= 1.30
  - Cost x1.5 PF >= 1.20
  - t_block >= 1.50
  - DSR(N_eff) >= 0.80

If all criteria pass -> promotion PR.
If any fail -> remain paper. **No sequential second look is allowed for the same frozen run.**

## Frozen accumulator workflow

The accumulator freezes exact model bytes, config snapshot, variant overrides,
feature manifest and start/minimum policy. Its manifest cannot be overwritten with
a different policy; candle transitions are idempotent append-only events.

```bash
python -m scripts.paper_accumulate create-manifest \
  --asset XAUUSD --variant wide_trend_filtered \
  --model-path output/models/xauusd_direction_model.joblib \
  --manifest config/paper/xauusd_wide_trend_filtered.json \
  --start 2026-08-08 --min-trades 50

python -m scripts.paper_accumulate run \
  --manifest config/paper/xauusd_wide_trend_filtered.json \
  --db-path data/paper_forward.sqlite
```

Liveness only (never reads PnL/PF/outcome payloads):

```bash
python -m scripts.paper_accumulate status \
  --manifest config/paper/xauusd_wide_trend_filtered.json \
  --db-path data/paper_forward.sqlite
```

Telegram exposes the same safe counter through `/paper` when
`PAPER_MANIFEST_PATH` and `PAPER_LEDGER_DB` are configured.

After 50 closed trades, perform the single read:

```bash
python -m scripts.run_live_forward_validation \
  --manifest config/paper/xauusd_wide_trend_filtered.json \
  --paper-db data/paper_forward.sqlite \
  --out logs/xauusd_wide_live_forward_once.json \
  --force
```

The command appends `validation_read` before loading outcome payloads. A second run
for the same manifest is refused, including after a crash following the marker.