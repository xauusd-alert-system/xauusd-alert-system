# Fold-local binary fallback for an absent `no_trade` class

The original three-class label-space fix makes a GBPUSD training window with
semantic labels `{short=0, long=2}` trainable by remapping it to binary labels
`{0,1}`. That prevents XGBoost's non-contiguous-class failure, but it revealed a
second issue: at GBPUSD's H1 / 36-bar barrier settings, `no_trade` is so rare
that most folds (including their calibration slices) do not contain it.

## Behaviour

`scripts.run_backtest._maybe_downgrade_three_class()` now checks the raw fold
labels before `build_training_matrix()`:

- it applies only when the effective asset model sets `include_zero_class: true`;
- when the fraction of raw `label == 0` rows is below
  `assets.<asset>.model.min_no_trade_frac` (default `0.01`), it makes a deep,
  **fold-local** configuration copy with `include_zero_class: false` and marks
  it `include_zero_class_effectively_binary: true`;
- labels are then built as the normal binary short/long set and the calibration
  path can calibrate two actual directional classes;
- the on-disk config remains three-class. A later fold with enough no-trade data
  continues to use the configured three-class mode.

The fallback logs one clear informational line per affected fold. Trainer
warnings from repeated calibration fallbacks are deduplicated by warning family,
and the old `{0,2}` remap warning is suppressed when the fold was intentionally
made binary.

## Scope and recommendation

This is a backtest safety and observability improvement, not proof that GBPUSD
has a useful three-class model. Production training still follows the configured
model flag. For the current GBPUSD barrier distribution, the durable choice is
usually:

```yaml
assets:
  GBPUSD:
    model:
      include_zero_class: false
```

Alternatively, redesign the labels (horizon/barriers/neutral region) until
`no_trade` has material support, then validate it out of sample. Do not enable
an intraday timeframe merely to address this label issue.
