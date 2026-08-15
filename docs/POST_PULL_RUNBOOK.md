# After `git pull`: operator runbook

## 1. Synchronize and keep execution frozen

```powershell
git pull origin arena/01a0068f-xauusd-alert-system
git status --short
python -m pip install -r requirements.txt
python -m compileall -q config contracts data model paper scripts tests
pytest -q
```

Expected branch configuration remains:

```yaml
deployment.mode: research
retraining.enabled: false
execution.enabled_assets: []
```

Do not change these before copied-DB validation and a reviewed promotion commit.

## 2. Configure identities and control security

Set secrets outside Git:

```powershell
setx TELEGRAM_ADMIN_CHAT_ID "<owner-chat-id>"
setx DASHBOARD_CONTROL_TOKEN "<long-random-token>"
```

`DRY_RUN` is only an extra brake. `deployment.mode` and the execution allowlist are
the actual routing authority. Web UI has no broker controls; emergency execution
control remains in authenticated Telegram.

## 3. Historical pre-lock validation

Create a copy if needed:

```powershell
Copy-Item data/market_data_mt5.sqlite data/market_data_ab_20260815.sqlite
```

Run target comparison:

```powershell
python -m scripts.run_backtest --asset XAUUSD --timeframe M15 --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 --label-event barrier
python -m scripts.run_backtest --asset XAUUSD --timeframe M15 --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 --label-event traded
```

Run MTF and family revalidation:

```powershell
python -m scripts.compare_mtf_references --asset XAUUSD --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 --output logs/mtf_reference_comparison.json
python -m scripts.deflated_sharpe --asset XAUUSD --db-path data/market_data_ab_20260815.sqlite --variants current,wide,wide_trend_filtered,null --end-date 2026-08-08 --historical-trials 738
```

Train auditable A/B artifacts only with the cutoff:

```powershell
python -m scripts.train_mt5 --symbol XAUUSD --timeframe M15 --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 --label-event barrier --output output/models/ab/xauusd_barrier.joblib
python -m scripts.train_mt5 --symbol XAUUSD --timeframe M15 --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 --label-event traded --output output/models/ab/xauusd_traded.joblib
```

Commit a new dated candidate decision. Old `wide_trend_filtered` figures are invalid.

## 4. Only after new pre-registration

Create a schema-v2 frozen manifest and start paper accumulation as documented in
`docs/CANDIDATE_WIDE_TREND_FILTERED.md`. Do not run the one-time validator until the
minimum is reached. The validator requires `--force` and permanently writes the
`validation_read` event first.

## 5. Primary ledgers

Set `TRADE_LOG_DB_PATH` to the operational SQLite file. New runs create:

- `trading_events`: immutable hash-chained signal/decision/order/management/PnL truth;
- `execution_fills`: empirical requested/fill/rejection/latency measurements;
- `signal_log`: secondary query-friendly signal projection.

Check execution distributions with `scripts.execution_cost_report`. Dashboard Monte
Carlo reads `trading_events.position_closed`, not Telegram or a hypothetical sample.

## 6. Optional Telegram archive import

```powershell
python -m scripts.import_telegram_archive exports\messages*.html --db-path data\channel_archive.sqlite
```

Imported records remain `unlinked`. The importer deliberately computes no WinRate.
Link messages to immutable signal IDs and broker position tickets before research.

## 7. Promotion requires a separate reviewed commit

A future promotion must set an explicit mode (`paper`, `demo_systematic`, or later
`live_systematic`), provide mandatory admin identity/live approval, and populate the
execution allowlist. Never combine promotion with model/threshold changes.
