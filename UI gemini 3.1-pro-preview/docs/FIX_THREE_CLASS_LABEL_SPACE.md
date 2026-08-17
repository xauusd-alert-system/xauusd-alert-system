# Fix — three-class label space with a missing class (GBPUSD walk-forward crash)

**Date:** 2026-08-11
**Symptom (reported from a real multi-asset run):**

```
python -m scripts.run_backtest --asset GBPUSD
...
File "model/trainer.py", line 260, in train_model
    model.fit(X_train, y_train, sample_weight=sw)
ValueError: Invalid classes inferred from unique values of `y`.
            Expected: [0 1], got [0 2]
```

XAUUSD and BTCUSD finished, EURUSD finished, **GBPUSD aborted at the 3rd fold**,
and because the exception escaped all the way to `main()` every remaining fold —
and the XAGUSD run queued behind it — was lost.

## Root cause

`build_training_matrix` encodes the three-class model as
`{0: short, 1: no_trade, 2: long}`. GBPUSD is the only asset that enables it
(`assets.GBPUSD.model.include_zero_class: true`).

XGBoost's scikit-learn wrapper infers `n_classes` from `np.unique(y)` of the data
it is handed and requires the labels to be **contiguous** `[0 .. n_classes-1]`.
A training window containing **no `no_trade` rows** therefore arrives as classes
`[0, 2]`: two classes, so XGBoost expects `[0, 1]`, gets `[0, 2]` and raises.

This window is common rather than exotic: the triple-barrier labeller only emits
label `0` when **neither** barrier is touched inside the horizon, which on GBP's
H1 / `horizon_candles_n: 36` / ATR-scaled barriers is rare. In the reported run
the first two folds happened to contain at least one such row and the third did
not.

Two related cases existed in the same code path:

| observed classes | before | after |
|---|---|---|
| `{0, 1, 2}` | trains, 3-class | unchanged, trains 3-class |
| `{0, 2}` (no `no_trade`) | **crash** (`got [0 2]`) | trains as binary short/long |
| `{1, 2}` (no short) | **crash** (`got [1 2]`) | refused explicitly, fold degraded |
| `{0, 1}` (no long) | trained, but `p_long` silently returned `P(no_trade)` | refused explicitly, fold degraded |

The `{0, 1}` row is the nastier one: it did **not** crash. `ModelPredictor`
decodes a two-column model as `p_short = P(class 0)`, `p_long = P(class 1)`, so a
window with no long outcomes produced a "p_long" that was really the
no-trade probability — a wrong directional signal, silently.

## Fix

`model/trainer.py`

* `DegenerateLabelSpaceError` — a typed, catchable error for the "this window
  cannot produce an honest directional model" **data** condition.
* `_normalize_label_space()` / `normalize_label_space()` — maps the semantic
  label space onto a contiguous, decodable one:
  * `{0,1,2}` → unchanged (predictor keeps exposing `p_no_trade`);
  * `{0,2}` → remapped `{0→0, 2→1}`, i.e. exactly the binary encoding
    `ModelPredictor` already decodes as `p_short`/`p_long`. Honest: no `no_trade`
    example existed in the window, so there is no `no_trade` mass to report.
    Logged as a warning, and recorded on the model as
    `_label_space_no_trade_absent` (mirrors the existing
    `_is_honest_placeholder` convention);
  * `{0,1}` / `{1,2}` → `DegenerateLabelSpaceError` instead of a silently
    mis-decoded probability.
* `train_model()` normalizes once, then fits via the new `_fit_classifier()`.
  The split matters: the mapping is **not** idempotent by value (a normalized
  binary `{0,1}` is indistinguishable from a three-class window holding only
  `{short, no_trade}`), so it must run exactly once per training set —
  `calibrate_model()` normalizes up front and refits internally through
  `_fit_classifier()`, never through `train_model()`.
* `calibrate_model()` now requires both the fit slice and the purged held-out
  slice to carry the **full** class set instead of merely "2+ classes". A slice
  that dropped one class of a three-class window would otherwise hand XGBoost
  the same non-contiguous labels *inside* `CalibratedClassifierCV`, and leave a
  per-class sigmoid fitted against a probability column that does not exist.
  For the binary space this is equivalent to the previous `nunique() < 2` check,
  so the two-class path is byte-for-byte unchanged in behaviour.

`scripts/run_backtest.py`

* `strategy_fn` catches `DegenerateLabelSpaceError` and degrades **only that
  fold** to the neutral `0.5 / 0.5` (which can never pass a filter, so the fold
  contributes no trades) with a printed warning. One pathological window can no
  longer destroy a multi-fold, multi-asset run. Any *other* exception still
  propagates — genuine defects stay loud.

Because the fix lives in the shared `train_model` / `calibrate_model` entry
points, every caller inherits it, including the **production training path**
(`scripts/train_mt5.py`, `scripts/deploy_guard.py`, `scripts/deflated_sharpe.py`,
`scripts/backtest_pooled.py`, `scripts/retrain_with_real_trades.py`), which
would have failed identically on a GBP dataset without `no_trade` rows.

## Consequence worth knowing

Since label `0` is rare at GBP's H1/36 settings, `include_zero_class: true` for
GBPUSD is largely **inert**: most windows will now train as a binary short/long
model (logged each time). If the intent is a genuinely three-class GBP model, the
labelling horizon/barriers need to produce a materially sized `no_trade` class
first — check the class balance before reading anything into the flag.

## Verification

* The pre-fix failure was reproduced exactly (`Expected: [0 1], got [0 2]`) and
  confirmed to disappear with the fix.
* Direction semantics were checked explicitly through the remap: for a feature
  that fully determines the direction, `p_long` averages **0.97** on
  long-favourable rows vs **0.04** on short-favourable ones (i.e. the remap does
  not invert or scramble the sides).
* `python -m pytest` → **447 passed** (baseline before this change: 439).

New regression tests:

* `model/tests/test_trainer.py` — `normalize_label_space` pass-through, remap,
  refusal and single-class cases; end-to-end train → calibrate → save → predict
  for a `{0,2}` window (asserting direction is preserved); and a healthy
  `{0,1,2}` window still exposing `p_no_trade`.
* `scripts/tests/test_gbp_fix_smoke.py` — GBPUSD walk-forward fold with no
  `no_trade` row trains instead of aborting (with a probe asserting the fold
  really reaches the trainer with classes `[0, 2]`, so the test cannot pass
  vacuously), and a fold missing a direction degrades to a no-signal fold.
