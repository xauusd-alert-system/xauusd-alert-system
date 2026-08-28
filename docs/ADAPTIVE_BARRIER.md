# Adaptive Barrier Research Comparison (Task 9)

Research comparison of the **fixed** vs **adaptive** triple-barrier holding
period on XAUUSD. **No production decision is made here** — the retrain
decision belongs to the owner.

## Setup

- Command (both runs):
  `python scripts/run_backtest.py --asset XAUUSD --label-event traded --end-date 2026-08-08 --no-journal`
- Run A (`results/adaptive_false.csv`): `labeling.adaptive_holding: false` (baseline).
- Run B (`results/adaptive_true.csv`): `labeling.adaptive_holding: true`
  (`adaptive_high_vol_pct: 0.02`, `adaptive_mid_vol_pct: 0.01`) — set only for
  the run, then reverted to `false`.
- Label event: `traded`; data cut at `--end-date 2026-08-08`
  (63,286 of 64,381 M15 candles, last bar 2026-08-07 20:45 UTC).

## Results: Fixed vs Adaptive

| Metric                       | Fixed (baseline) | Adaptive | Delta |
|------------------------------|------------------|----------|-------|
| Total PnL ($)                | -958.22          | -958.22  | 0     |
| Trades (summed)              | 334              | 334      | 0     |
| Win rate (pooled, approx.)   | ~58.4%           | ~58.4%   | 0     |
| Median PF (valid folds)      | 0.915            | 0.915    | 0     |
| Positive folds               | 2/6 (33.3%)      | 2/6 (33.3%) | 0   |
| Empty folds                  | 7                | 7        | 0     |
| Sign test vs 50%             | z=-0.816, p=0.89 | identical | 0    |

Per-fold metrics are byte-identical (`fc results\adaptive_false.csv
results\adaptive_true.csv` → IDENTICAL).

## Why the runs are identical

The adaptive switch is a **no-op on this asset/sample** because the XAUUSD
volatility ratio never crosses the configured thresholds:

```
vol_pct = atr / price  (proxied from ohlcv_m15 close, 14-bar mean abs change)
mean   = 0.176%
p99    = 0.660%
max    = 0.941%
```

Both thresholds (`adaptive_mid_vol_pct = 0.01` = 1%, `adaptive_high_vol_pct =
0.02` = 2%) sit above the maximum observed vol_pct, so every bar keeps the
full base horizon (`horizon_candles_n = 36`) and the label space is unchanged.

The thresholds were calibrated as round generic values; gold on M15 is a
low-volatility-ratio instrument (its ATR is a few dollars against a ~$4,400
price). For the switch to ever engage on XAUUSD, the thresholds would need to
be roughly 10x lower — but that is a **pre-registration decision** (owner),
not something to back-fit after seeing these results.

## Conclusion / recommendation

- On XAUUSD M15 with the current thresholds the adaptive holding period
  changes nothing: baseline behaviour is preserved exactly.
- The mechanism itself is covered by tests
  (`labeling/tests/test_adaptive_holding.py`, 17 tests: thresholds, NaN/price
  guards, min-horizon, byte-identical regression when off, horizon variation
  on synthetic mixed-vol frames, determinism).
- `labeling.adaptive_holding` remains **false** in `config/config.yaml`
  (production behaviour unchanged). No retrain is warranted by this
  comparison; if the owner wants the adaptive horizon to be meaningful on
  gold, the thresholds must be re-registered FIRST and the comparison
  re-run — never tuned against locked/live-forward outcomes.

## Artifacts

- `results/adaptive_false.csv` — run A fold metrics (not committed; results/ is git-ignored).
- `results/adaptive_true.csv` — run B fold metrics (not committed).
- `logs/backtest_xauusd_traded.csv` — runner output of the last run (regenerated).
