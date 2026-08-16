# DeepSeek v4-pro-max re-audit (2026-08-16)

## Confirmed and retained

- `train_mt5.build_full_df` passes `asset_key` and now also resolves per-asset
  overrides internally for direct callers such as deploy guard/layer validation.
- XAUUSD `labeling.event: traded` reaches production labeling.
- Production and primary walk-forward paths use index-aligned uniqueness weights.
- Calibration receives the same explicit sample-weight policy.
- SQLite retains broker `spread`/`real_volume`; execution attempts have a separate
  empirical fill/rejection/latency ledger.
- Only the event-sourced `data/paper_ledger.py` implementation exists. No conflicting
  `data/paper_ledger/` package or old `paper_accumulate_wide_filtered.py` remains.
- Dashboard account fallback no longer fabricates a $100,000 balance.

## Additional defects found during re-audit

1. `train_mt5` lacked a raw pre-feature `--end-date` cutoff. Added; cutoff is stored
   in bundle metadata.
2. A frozen manifest could accept a model trained into the live-forward period.
   It now rejects missing/overlapping data-period metadata and mismatched timeframe.
3. Paper validation had no explicit CLI burn confirmation and had a concurrent-read
   race. `--force` is now required; only the process that creates the unique
   `validation_read` marker may read outcomes.
4. SQLite paper tables were append-only by convention only. UPDATE/DELETE prevention
   triggers now enforce the contract in the database.
5. Dashboard JavaScript had a duplicate `let bg` declaration and unsafe `.join()` /
   `.toFixed()` calls for unavailable data. Fixed and syntax-checked with Node.
6. Static correlation, sample sentiment, hypothetical Monte Carlo, random charts,
   hard-coded institutional metrics, and a fake-success web `closeall` response were
   still present. They were removed. Missing real data is now `available: false`.
7. The traded label/backtester used the completed entry-bar ATR at next-open and
   ignored signal-step clamps, while live inference used signal-bar ATR/clamps. The
   shared contract now freezes step from the closed signal bar, applies fixed/min/max
   settings, and applies TP/stop multipliers once across label, both backtest engines,
   paper and live order setup.
8. Synthetic pooled/feature-selection fallback labeling omitted the per-asset config
   and `asset_key`. Both now honor the same target contract.
9. A successful MT5 result with zero/absent fill price could manufacture huge
   slippage. Such fields are now stored as unknown, not as a numeric fill.

## Calibration decision

DeepSeek suggested replacing the explicit split with `CalibratedClassifierCV(cv="prefit")`.
That change was **not** applied. The pinned sklearn API accepts `sample_weight` in
`CalibratedClassifierCV.fit`; the current explicit single purged time split weights
both the cloned base-estimator fit and held-out calibrator. A prefit path would make
calibrator weighting ambiguous and is deprecated in newer sklearn in favor of a
frozen estimator. Regression tests retain strict temporal ordering and purge gap.

## Verification

- Full suite: `535 passed, 11 warnings`.
- Python compileall: clean.
- Dashboard JavaScript `node --check`: clean.
- Locked/live-forward outcomes were not read.

## Still requires the owner's real environment

- legacy-vs-traded A/B on a copied DB ending strictly at 2026-08-08;
- `[M15,H1]` vs `[H1,H4]` pre-lock comparison;
- schema-v2 XAUUSD model trained with `--end-date 2026-08-08`;
- frozen manifest creation and forward accumulation;
- empirical broker-cost sample collection and review.
