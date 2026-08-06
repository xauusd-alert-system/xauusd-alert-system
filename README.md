# xauusd-alert-system

A multi-asset, machine-learning trading and alerting system for MetaTrader 5. It
turns live M5 candles into calibrated directional signals, gates them through a
regime/session/news meta-filter, and can either alert on Telegram or place and
manage orders automatically. XAU/USD (gold) is the flagship instrument, but the
same pipeline runs for XAG/USD, BTC/USD, EUR/USD, and GBP/USD.

> ⚠️ **Risk disclaimer.** This is research/automation software for trading
> leveraged instruments. It can and does lose money — see the honest
> walk-forward baselines in [`docs/benchmarks.md`](docs/benchmarks.md), which are
> net-negative on several assets. Use a demo account first and never risk capital
> you cannot afford to lose. Nothing here is financial advice.

## How it works

The system is a strictly layered, causal pipeline. Each stage only ever reads
information available at the close of the current candle — the one place that is
intentionally forward-looking (offline label generation) is quarantined in
`labeling/` and never reaches inference.

```
MT5 candles ─▶ features ─▶ regime ─▶ model ─▶ ensemble/meta-filter ─▶ signal
 (data/)      (features/)  (regime/) (model/)     (model/ensemble.py)     │
                                                                          ├─▶ Telegram alert (alerts/)
                                                                          └─▶ MT5 auto-trader (execution/)
```

| Layer | Package | Responsibility |
|-------|---------|----------------|
| Data | `data/` | MT5 provider, ingestion, session tagging, SQLite storage, trade/signal logging, news filter |
| Features | `features/` | Indicators, candle anatomy, market structure, multi-timeframe confluence (all causal) |
| Regime | `regime/` | Rule-based regime classifier (trend / range / compression / reversal-watch / no-trade) |
| Labeling | `labeling/` | Triple-barrier, ATR-scaled label generation for supervised training (offline only) |
| Model | `model/` | XGBoost (or RF / LightGBM / voting) trainer with purged, time-ordered calibration; inference; ensemble meta-filter |
| Backtest | `backtest/` | Event-driven backtester + walk-forward harness + metrics |
| Realtime | `realtime/` | The inference pipeline and a FastAPI `/signal` service |
| Execution | `execution/` | Live MT5 auto-trader, breakeven/partial-TP management, institutional risk manager / circuit breaker |
| Alerts | `alerts/` | Telegram alert bot + interactive control bot (`/pause`, `/resume`, `/closeall`) |
| Simulation | `simulation/` | A virtual limit-order-book market + MT5 shim for deterministic, offline end-to-end runs |
| Scripts | `scripts/` | Backfill, train, backtest, simulate, overnight retrain, deploy guard, reporting |

## Documentation

- **[Technical Specification / Техническое Задание (ТЗ)](TZ.md)** (`TZ.md` / `docs/TZ.md`): исчерпывающее техническое описание всех подсистем, формул, потоков данных, риск-менеджмента и контрактов исполнения.
- **[TODO & Roadmap (План работ и статус)](TODO.md)** (`TODO.md` / `docs/TODO.md`): матрица готовности подсистем, чеклист развертывания и дорожная карта развития (Phase 0–6 completed, Phase 7+ roadmap).
- **[Strategy Benchmarks & Validation](docs/benchmarks.md)** (`docs/benchmarks.md`): протокол честной валидации без утечек данных, walk-forward бейзлайны по активам и журнал изменений.

## Requirements

- **Python 3.12+** (the pinned dependency stack, e.g. `numpy==2.5.1`, requires it).
- For **live trading only**: a running **MetaTrader 5** terminal on **Windows**.
  `MetaTrader5` ships Windows-only wheels and is guarded by a platform marker in
  `requirements.txt`, so it is skipped on Linux/macOS. Backtests, training, and
  the virtual simulation run anywhere via the bundled MT5 shim.

## Setup

```bash
# 1. Install dependencies (Linux/macOS or Windows)
pip install -r requirements.txt

# 2. Configure secrets and runtime options
cp .env.example .env
# then edit .env — at minimum TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID if you want
# alerts, and DATA_MODE (live|mock). NEVER commit .env.

# 3. Run the tests
pytest -q
```

All non-secret configuration lives in [`config/config.yaml`](config/config.yaml)
(single source of truth, loaded via `config/loader.py`). Secrets and environment
switches live in `.env` (see `.env.example`). Per-asset overrides for the
ensemble thresholds, contract sizes, slippage, and labeling sit under the
`assets:` section of the YAML.

## Common tasks

```bash
# Seed / backfill historical candles into SQLite
python -m scripts.seed_db --symbol XAUUSD --days 90
python -m scripts.backfill_data            # incremental, idempotent upsert

# Train models for every enabled asset
python -m scripts.train_all_assets

# Walk-forward backtest a single asset (writes logs/backtest_<asset>.csv)
python -m scripts.run_backtest --asset XAUUSD --timeframe M5 --db-path data/market_data_mt5.sqlite

# Run the FastAPI inference service (exposes GET /signal and /health)
uvicorn realtime.app:app --reload

# Deterministic, offline end-to-end run against the virtual market (no MT5 needed)
python -m scripts.run_simulation

# Live/paper multi-asset auto-trader + Telegram control bot (needs MT5 on Windows)
DRY_RUN=1 python -m scripts.run_bot        # DRY_RUN=1 logs orders without sending

# Overnight self-improvement pipeline (backfill → backtest → retrain → deploy-guard → report)
python -m scripts.overnight
```

## Testing & CI

The suite (173 tests at time of writing) is run with `pytest -q` from the repo
root — `pyproject.toml` sets `pythonpath = ["."]` so the packages import without
installation. A GitHub Actions workflow (`.github/workflows/ci.yml`) runs the
suite on every push to `master` and every pull request.

Feature causality and no-look-ahead invariants are locked in by dedicated tests
(`features/tests/test_no_lookahead.py`, `model/trainer.py`'s purged calibration).
Please keep them green, and update `docs/benchmarks.md` whenever a change affects
model or validation behavior.

## Repository layout

```
alerts/       Telegram alert + control bots
backtest/     Event-driven backtester, walk-forward, metrics
config/        config.yaml (single source of truth) + loader
data/         MT5 provider, ingestion, storage, session tagging, news filter, loggers
deploy/        systemd units for the overnight timer
docs/          benchmarks.md (honest validation protocol + baselines)
execution/     Live MT5 auto-trader + institutional risk manager
features/      Causal technical/structure/confluence features
labeling/      Offline triple-barrier label generation
model/         Trainer, predictor, ensemble meta-filter
realtime/      Inference pipeline + FastAPI service
regime/        Rule-based regime classifier
scripts/       CLIs: backfill, train, backtest, simulate, overnight, deploy guard
simulation/    Virtual LOB market + MT5 shim for offline runs
```
