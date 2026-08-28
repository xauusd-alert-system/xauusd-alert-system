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

---

# Preregistered Re-run: Per-Asset Percentile Thresholds (2026-08-29)

Second preregistered comparison. Contract: [docs/ADAPTIVE_PREREGISTRATION.md](ADAPTIVE_PREREGISTRATION.md)
(thresholds fixed BEFORE the runs from [docs/VOL_PCT_DISTRIBUTION.md](VOL_PCT_DISTRIBUTION.md)).
`adaptive_holding: false` restored in `config/config.yaml` after the runs.

## Setup

- Command: `python scripts/run_backtest.py --asset <A> --end-date 2026-08-08 --no-journal`
  (XAUUSD uses its configured `event: traded`; EURUSD/GBPUSD use their configured
  `barrier`/`atr_scaled` labeling — no `--label-event` override).
- Run A (Fixed): `adaptive_holding: false` → `results/<asset>_false3.csv`.
- Run B (Adaptive): `adaptive_holding: true` + per-asset thresholds
  (high=p95, mid=p75 of vol_pct per asset) → `results/<asset>_true3.csv`.
- Data cut: `--end-date 2026-08-08` (XAUUSD 63,286 M15 candles;
  EURUSD/GBPUSD 16,632 H1 candles each).

## Results: Fixed vs Adaptive (per asset)

| Metric | XAUUSD Fixed | XAUUSD Adaptive | EURUSD Fixed | EURUSD Adaptive | GBPUSD Fixed | GBPUSD Adaptive |
|--------|-------------|-----------------|--------------|-----------------|--------------|-----------------|
| Total PnL ($) | -959.22 | -1015.49 | -1059.93 | -947.13 | -3000.59 | **-46.79** |
| Trades | 334 | 285 | 268 | 224 | 471 | 67 |
| Win rate (pooled, valid folds) | 55.1% | 55.1% | 48.5% | 45.5% | 58.8% | 65.7% |
| Median PF (valid folds) | 0.915 | 0.635 | 0.520 | 0.515 | 0.490 | **1.000** |
| Positive valid folds | 2/6 | 2/6 | 0/7 | 0/6 | 0/11 | **3/7** |
| Sign test vs 50% | z=-0.816, p=0.89 | z=-0.816, p=0.89 | p=1.0 | p=1.0 | p=1.0 | z=-0.378, p=0.77 |

## Verdicts per the preregistered contract

Success criteria: primary = positive valid folds ≥ 3/6 AND median PF(valid) > baseline;
secondary = Total PnL > baseline; fail = byte-identical OR worse on primary and
secondary; marginal = primary not met but PnL improved.

| Asset | Verdict | Rationale |
|-------|---------|-----------|
| XAUUSD | **FAIL** | Primary: 2/6 (< 3/6) and median PF 0.635 < 0.915. Secondary: PnL -1015.49 < -959.22. Not identical (labels changed: 53,895 vs 53,970 informative bars) but worse on both criteria. |
| EURUSD | **MARGINAL** | Primary not met: 0/6 (< 3/6), median PF 0.515 < 0.520 (effectively flat). Secondary met: PnL -947.13 > -1059.93 (+112.80). Owner's call. |
| GBPUSD | **SUCCESS** | Primary met: 3/7 (≥ 3/6) AND median PF 1.000 > 0.490. Secondary met: PnL -46.79 > -3000.59 (+2953.80). Trade count collapsed 471 → 67: the adaptive horizon largely suppresses the FX noise-trades that lost money in the fixed-horizon baseline. |

## Notes and caveats

1. Not a single run is byte-identical — the per-asset percentile thresholds do
   engage (25% of bars halve, 5% quarter by construction), unlike the old
   generic 0.01/0.02 thresholds above.
2. GBPUSD's improvement is the standout, but it comes with a ~7x drop in trade
   frequency; that changes exposure and cost profile, and a different label
   space would require a retrain (contract_version bump) before prod trust.
3. XAUUSD gets WORSE with the adaptive horizon: shortening the barrier window
   in turbulent bars is harmful for the `traded` event on gold.
4. Thresholds were NOT tuned after seeing results — values come from the
   preregistration contract (p95/p75 of the pre-cutoff vol_pct distribution).
5. Both docs count as preregistered research (criterion ≥3 of the TZ: this is #2
   together with the vol_pct distribution study).

## Recommendation

- `adaptive_holding` stays **false** in prod for XAUUSD and EURUSD.
- GBPUSD adaptive is the only candidate worth the owner's attention; a decision
  to adopt it belongs to the owner and would require retrain + revalidation
  (never flip labels without retraining).
- Retrain decision: **owner's call** — none of the verdicts alone forces one.

## Artifacts (2nd run)

- `results/xauusd_false3.csv` / `results/xauusd_true3.csv` (+ `_fold_summary.csv`)
- `results/eurusd_false3.csv` / `results/eurusd_true3.csv` (+ `_fold_summary.csv`)
- `results/gbpusd_false3.csv` / `results/gbpusd_true3.csv` (+ `_fold_summary.csv`)
  All under git-ignored `results/` — not committed.
- `logs/backtest_<asset>.csv` — runner output of the last (GBPUSD) run, regenerated per run.
