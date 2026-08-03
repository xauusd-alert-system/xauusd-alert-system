# Overnight model "self-improvement" pipeline

`python -m scripts.overnight` runs the whole retrain-and-validate routine while
you sleep. It is a thin orchestrator around existing entry points, so each stage
is isolated: one stage failing doesn't kill the rest of the night.

## What it does, in order

| # | Stage | Entry point | Writes |
|---|-------|-------------|--------|
| 1 | Backfill fresh MT5 data (incremental) | `scripts.backfill_data` | SQLite candles |
| 2 | Walk-forward backtest per enabled asset | `scripts.run_backtest` | `logs/backtest_*.csv` |
| 3 | Fresh retrain of all assets | `scripts.train_all_assets` | `output/models/*.joblib` |
| 4 | Final retrain + real executed trades | `scripts.retrain_with_real_trades` | `output/models/*.joblib` |
| 5 | Portfolio summary report | `scripts.summary_report` | stdout |
| 6 | Telegram summary | `alerts.telegram_bot` | message |

The final model files left on disk come from stage 4 (history + real trades),
which is what you want to wake up to.

## Requirements

- **MT5 terminal running** on the machine (FxPro). Stage 1 reads live candles
  through `data.mt5_provider`; config already sets `market_data.provider: mt5`.
  If MT5 isn't running, stage 1 logs a warning and the rest still runs on the
  existing SQLite history.
- Install Python deps: `pip install -r requirements.txt`.

## Run manually (this is what "just turn it on tonight" means)

```bash
python -m scripts.overnight
```

Stages are on by default. Skip any with env vars, e.g.:

```bash
OVERNIGHT_NO_BACKFILL=1 OVERNIGHT_NO_TELEGRAM=1 python -m scripts.overnight
OVERNIGHT_BACKFILL_DAYS=7 python -m scripts.overnight   # only pull 7 days of fresh data
```

## Auto-run every night

### Option A — systemd timer (recommended on a server / always-on box)

1. Edit `overnight.service` and replace `{{PROJECT_ROOT}}`, `{{PYTHON}}`,
   `{{USER}}`.
2. Install:
   ```bash
   sudo cp deploy/overnight/overnight.service /etc/systemd/system/
   sudo cp deploy/overnight/overnight.timer    /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now overnight.timer
   ```
3. Inspect: `systemctl list-timers overnight.timer` · logs:
   `tail -f logs/overnight.log`.

The timer fires at **03:00 daily** (see `overnight.timer` → `OnCalendar`).

### Option B — cron

```cron
# every day at 03:00, cd into the repo and run the pipeline
0 3 * * *  cd /path/to/xauusd-alert-system && \
           OVERNIGHT_BACKFILL_DAYS=45 /usr/bin/python3 -m scripts.overnight \
           >> logs/overnight.log 2>&1
```

### Option C — just the scheduler for live signals (not part of overnight)

If instead of training you want the realtime alert bot running 24/7:

```bash
python -m scripts.run_scheduler
```
