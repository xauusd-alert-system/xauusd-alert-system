# Audit 2026-08-10 — Fix Status

Mapping of every finding from the independent SWOT/quant audit (snapshot `04ad520`)
to its resolution. Code bugs are fixed; methodology/data-dependent items are
documented with an explicit reason they were not silently changed.

## Fixed (code)

| ID | Finding | Fix |
|----|---------|-----|
| W1 | Exit costs not charged (half spread + one slippage instead of full round-trip) | `model/ensemble_backtest.py`: `_apply_exit_cost()` on every exit path (TP1/TP2/TP3/trailing/stop/progress/timeout) -> full spread + two slippages. Test added. |
| W2 | 50/30/20 scale-out unimplementable on 0.01 lot (closes whole position / zero-volume TP2) | `execution/mt5_trader.py`: live volume from config; `_scaleout_volume()` quantizes to `volume_step`/`volume_min`, skips unfillable tranches. Tests added. |
| W3 | No purge/embargo between train and test (labels resolve into test) | `backtest/walk_forward.py`: purge horizon + `embargo_candles`; `run_backtest.py`: sample weights by label uniqueness. Test added. |
| W4 | Intrabar double-touch resolved optimistically (TP before stop) | `model/ensemble_backtest.py`: conservative stop-first on same-candle double-touch (mirrors `engine.py`). Test added. |
| W5 | Two engines, two money models (price vs money PnL) | `backtest/engine.py`: baseline now reports money PnL (volume × point_value_lot). |
| W6 | `engine.py` ignored per-asset spread/slippage/grid | `backtest/engine.py`: reads `assets.<key>.spread_usd/slippage_usd` + per-asset grid. |
| W7 | `compute_r_metrics` default lot/volume match no asset, ignored per-trade volume | `backtest/metrics.py`: honours a per-trade `volume` column. Test added. |
| W8 | Risk limits hard-coded (3/10), `execution.*` config dead | `execution/risk_manager.py`: reads `execution.max_concurrent_positions_global` / `max_daily_trades_per_asset`. Config updated. Test added. |
| W9 | `positions_get(magic=...)` breaks on real MT5 API | `execution/mt5_trader.py` + `scripts/telegram_admin.py`: `positions_get_by_magic()` filters in Python. Shim now rejects `magic` (TypeError) like the real API. Tests added. |
| W10 | Position management state / daily risk state lost on restart | `risk_manager.py` persists daily state to `logs/risk_state.json`; `mt5_trader.py` persists `active_trades` management state to `logs/live_management_state.json`. Tests added. |
| W11 | Telegram token could leak into logs (base_url already correct) | `alerts/telegram_bot.py`: `_redact()` strips the token from logged exceptions. Tests added. |
| W12 | `tp1_hit`/`tp2_hit` advanced before checking retcode | `execution/mt5_trader.py`: state advances only on successful partial close. |
| W15 | Ensemble magic numbers not in config | `model/ensemble.py` thresholds declared in `config.yaml` (`min_ml_probability`, `ml_confidence_floor`, `crypto_night_min_probability`). |
| W18 | `NameError` `db_path` in `run_backtest.main()` | `scripts/run_backtest.py`: `db_path` -> `args.db_path`. |
| N1 | `cvd` level depends on window length (train/serve skew) | `features/order_flow.py`: `cvd` anchored to a fixed trailing window. Tests updated/added. |
| N2 | Causality tests skipped order_flow/smart_money/fractional_diff/regime | `features/tests/test_no_lookahead.py`: coverage extended. |
| N3 | MT5 shim richer than real API (accepts `magic`) | `simulation/mt5_shim/...`: shim rejects `magic`; tests updated. |
| N5 | `diag_r_metrics` hard-coded 0.55 + regime set | `scripts/diag_r_metrics.py`: `_signal_mask` uses per-asset ensemble config. |
| N6 | Four independent R-formula implementations | `scripts/exit_profile.py`: derives per-trade fields from `trades_to_dataframe` (same source as `compute_r_metrics`). |
| N7 | `calculate_lot_size` rounded up to min_lot (contradicts risk_sizer) | `execution/portfolio_allocator.py`: returns 0 (=skip) below min, never rounds up. Test added. |
| N8 | `hierarchical_risk_parity` misnamed (it is inverse-variance) | `execution/portfolio_allocator.py`: honest docstring. |
| N9 | `VERY HIGH` branch unreachable in `calculate_delta_confidence` | `features/smart_money_metrics.py`: check order fixed. Test added. |
| N10 | MT5 server-time treated as UTC; `spread`/`real_volume` dropped | `data/mt5_provider.py`: optional `server_time_offset_hours`; preserves `spread`/`real_volume`. Config updated. Test updated. |
| N11 | Scale-out validator only in backtest | `execution/mt5_trader.py`: startup validation `raise_on_invalid=True`. |
| T7 | Sharpe/Sortino annualized by fixed sqrt(250) regardless of trade frequency | `backtest/metrics.py`: annualizes by the ACTUAL per-year trade frequency (`sqrt(trades_per_year)` from the entry timestamps), with a 250 fallback. Test added. |
| T8 | Broker contract size hard-coded in config | `execution/mt5_trader.py`: `_validate_contract_sizes()` warns on mismatch with live `trade_contract_size` / `volume_step`. |
| T11 | Bar-poll hard-coded to M5 for every asset (H1 signals re-queried every 5 min) | `execution/mt5_trader.py`: polls each symbol at its OWN asset timeframe (per-asset `timeframe`), not a global M5. |

## Documented (methodology / data-dependent — not code bugs)

| ID | Finding | Why not code-fixed |
|----|---------|--------------------|
| W13 | Backtest `fillna(0.0)` vs live no_trade on NaN warm-up | FIXED — see `run_backtest.py` (rows with NaN features -> neutral 0.5/0.5). |
| W14 | News guard only in live (no historical news feed) | Backtest bars >~7 days are skipped by `is_news_red_zone()`; no historical calendar feed exists. Documented in `config.yaml` (`apply_news_guard_in_backtest`). |
| W16 | Confidence thresholds incomparable across assets | Per-asset `min_confidence_to_alert` applied to a blended scale; 3-class models leave residual mass. Documented in `config.yaml`; `normalize_probs` is the opt-in lever (requires revalidation). |
| W17 | `sentiment_analyzer` dead in trading path | FIXED as an OPTIONAL default-off veto (`use_sentiment_guard`) + dashboard. Test added. |
| T1–T6, T9, T10, T12 | Benchmark-table integrity, walk-forward fold overlap treated as independent, doc test-count divergence, single-process, news-feed breadth | These require re-running on the real DB (not present in this clone) or are operational/documentation. Changing benchmark numbers without a re-run would fake results. `W3` (overlap) is fixed in code. `T7` and `T11` are now fixed in code (see above). |
| N4 | `cost_ratio` full round-trip vs PnL half | RESOLVED by W1 (both now use full round-trip). |

## Opportunities (O1–O7)

| ID | Opportunity | Status |
|----|-------------|--------|
| O1 | Diag scripts (`diag_r_metrics`, `exit_profile`) fall back to synthetic biased data with the flag only in a JSON sidecar | PARTIALLY FIXED: the synthetic marker is now printed loudly and written into the CSV artifact itself, so a fresh clone cannot mistake it for real measurements. Running them on the real DB requires the DB (not in this clone). |
| O2 | Fix W1+W4 re-solves the baselines | DONE (W1, W4 fixed in code). Re-running baselines needs the real DB. |
| O3 | BTCUSD candidate for capital | Requires real-DB re-measurement and the decision gate; not code. |
| O4 | Portfolio/risk layer implemented but disabled (`risk.enabled: false`) | Documented (see `TZ.md` §3.6 / README). Enabling changes live order sizes, so it is an explicit opt-in step, not silently turned on. |
| O5 | `fill_mode` / queue-loss measurement already in the engine | Already present (`ensemble_backtest.py`); measurement run needs real data. |
| O6 | Locked hold-out (2026-08-08 onward) is the one honest path | Config/documented; requires live-forward data accumulation. |
| O7 | Read contract size from the terminal instead of trusting config | DONE via T8 (`_validate_contract_sizes` reads `trade_contract_size`/`volume_step` and warns on mismatch). |

## Retraining required after these changes

The following alter the feature/label distribution a trained model sees, so
models must be retrained before trusting new results:
- N1 (anchored `cvd`) — feature definition changed.
- W13 (NaN warm-up rows now neutral instead of `0.0`).
- W1 (exit costs) — re-costs every benchmark (this is the point).

## Verification

`python -m pytest` -> **436 passed** (baseline on snapshot was 408).

Updated in the follow-up round: `T7` (frequency-based Sharpe/Sortino), `T11`
(per-asset bar polling), and `O1` (loud synthetic marker in diag reports/CSVs).
