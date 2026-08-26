# xauusd-alert-system — US Stocks Headliners signal-only scanner

Инженерное ТЗ, а не обещание прибыли. Бот только сканирует
и отправляет сигнал в Telegram; ордера в терминале — вручную пользователь.

---

## Быстрый старт

```powershell
git clone https://github.com/<org>/xauusd-alert-system.git; cd xauusd-alert-system
python -m venv venv; venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env   # заполните TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
```

- Существующие forex/crypto/MT5-модули продолжают работать в легаси-профилях.
  Для нового софта менять ничего не нужно — выбирается профиль.
- Токены не коммитятся (`.env` в `.gitignore`).

## Профили (config/loader.py)

| Профиль | Режим |
|---|---|
| `forex_legacy` | исторический дефолт (MT5-трейдер `scripts/run_bot.py`, control bot) |
| `crypto_legacy` | то же |
| `us_stocks_challenge` | **signal-only** — сканер → стратегия → risk-engine → Telegram → журнал; `execution.mode=disabled`, `DisabledExecutor` бросает на любом `submit()`, исполняющие точки входа отказываются стартовать |
| `replay` | офлайн-воспроизведение сессии из CSV (без сети) |

Выбор: `PROFILE` env или `--profile <name>` CLI (CLI выигрывает). `PROFILE` без значения = `forex_legacy` → легаси не задет.

Конфиг профиля: `config/us_stocks_challenge.yaml` — числа ТЗ §2 verbatim:
`account $1000 / цель +$80 / дневной лимит −$50 / общий −$100 / плечо 1:5 / номинал ≤$5000`; внутренние: риск $10/сделка, персональный стоп −$20, макс 2 сделки/день, 2 убытка подряд, profit-lock +$20, стоп новых входов за 25 мин до закрытия नियमित.

## Replay (офлайн)

CSV ожидается с колонками `symbol,ts,open,high,low,close,volume` (`ts` ISO или epoch).

```powershell
$env:PROFILE="replay"
venv\Scripts\python.exe -m usstocks.replay `
  --symbol-csv AMD=data/replay/AMD.csv `
  --benchmark-csv QQQ=data/replay/QQQ.csv `
  --watchlist AMD --is-tech AMD
# или часы-ofiured режим:
venv\Scripts\python.exe -m usstocks.replay --help
```

Replay не делает сетевых запросов (тест сторожит `socket()`) и печатает watchlist-вердикты, сигналы с sizing и коды отказов чеклиста.

Фикстуры синтетических сессий: `tests/fixtures/vwap_scenarios.py` (long/short/flat + бенчмарки).

## Live signal-only (US Stocks)

```powershell
# 1) софт уже установлен, .env заполнен
# 2) профиль раз и навсегда выбран signal-only:
$env:PROFILE="us_stocks_challenge"
# дневные переключения необязательны — бот спрашивает профиль при каждом старте

# раннер (в одном процессе: лонг-поллинг TG + сканирование NY-сессии):
venv\Scripts\python.exe -m usstocks.bot
```

Что он делает в NY-сессию (09:30–16:00, America/New_York, DST через `zoneinfo`):

1. Почтас правовой detached проверкой UTEX PROVIDER берёт 1m-свечи watchlist и бенчмарков (SPY/QQQ). Source reuse: `usstocks/data/utex_provider.py` (тот же HTTP-codebase, что и `challenge/manual/alerter.py`).
2. Premarket-rankер до открытия собирает top-3 watchlist (scoring ТЗ §7.1: gap, rel-volume, news-catalyst, ADVs, spread; фильтры min_price/gap/relvol/ADV/spread).
3. Стратегия **VWAP Pullback Continuation** (см. `docs/STRATEGY_VWAP_PULLBACK.md`) на закрытых 5m-барах: требует импульс ≥0.8% на ≥1.5x объёме, откат к VWAP ±0.10% на слабом объёме, подтверждающее закрытие за VWAP, higher-low, OR15 mid-фильтр и место до ключевого уровня ≥1.8R. ML `advisory_only` (нет права разрешать сделку).
4. Sizing: стоп первым → `floor($10/risk) ∩ floor($5000/entry)`. TP1=1R/TP2=2R.
5. Risk engine (шесть блокировок + стоп-день оператора) — порядок детерминирован (ТЗ §8).
6. Сигнал → Telegram (шаблон §9 с дисклеймером) → запись в SQLite; никаких `Executor.submit()` в графе (в тесте — `SpyExecutor`, который бы упал).

Executable entry points (`scripts/run_bot.py` — MT5-трейдер, `challenge/runner.py` — браузерная автоторговля, `challenge/stealth/runner_bridge.build_engine`) под signal-only профилем отказываются стартовать (`SystemExit 2`).

## Telegram

Один токен, один `getUpdates`-консьюмер (предупреждение как в control_bot.py — не запускайте два polling-бота одновременно).

Легаси-команды (`/status`, `/positions`, …) остаются администраторскими и не трогают ордера — см. `alerts/status_commands.py`.

Новые `/us_*` (только владелец: `TELEGRAM_ADMIN_CHAT_ID` fallback `TELEGRAM_CHAT_ID`):

| Команда | Что делает | Подтверждение |
|---|---|---|
| `/us_status` | сводка `RiskState` + журнал дня + стоп-день/вкл-сигналов | нет |
| `/us_watchlist` | последняя watchlist-карточка | нет |
| `/us_signals on`\|`off` | мастер-выключатель сканирования | нет |
| `/us_pnl <amt>` | ручной ввод PnL сделки | **да** (inline ✅/❌, TTL 5 мин) |
| `/us_win <amt>` | то же (+; последоват. лоссы сбрасывает) | да |
| `/us_loss <amt>` | то же (−; +1 к streak лоссов) | да |
| `/us_flat` | обнулить `active_symbol` | да |
| `/us_stop` | стоп-день (блокирует новые сигналы) | да |
| `/us_resume` | снять стоп-день | да |
| `/us_export [YYYY-MM-DD]` | CSV дневного журнала в `data/usstocks_export/` (+ `sendDocument`) | нет |

**Правило:** изменение P&L/статуса применяется ТОЛЬКО после «✅ Принял». ❌ или истечение TTL отменяют. Тот же код проверяет ТЗ §12.16 (два подтверждённых лосса подряд блокируют следующий скан `MAX_CONSECUTIVE_LOSSES`).

## Журнал и экспорт

SQLite `data/usstocks.sqlite` (`usstocks/journal.py`):

- `us_sessions` — по дню NY;
- `us_watchlist_snapshots` — каждый скрин watchlist с source/версия стратегии;
- `us_signals` (`pending`/…/`taken`) с метриками и passed/why JSON;
- `us_trade_outcomes` — win/loss/flat, PnL и R (`r_multiple = pnl / planned_risk`), `confirmed_by` (chat id);
- `us_risk_events` — каждый allow/deny с кодом.

```powershell
$env:PROFILE="us_stocks_challenge"
venv\Scripts\python.exe -c "from usstocks.journal import UsJournal; UsJournal('data/usstocks.sqlite').export_day_csv('2026-08-26','data/usstocks_export')"
```

Файл — `data/usstocks_export/us_signals_YYYY-MM-DD.csv` (pandas-friendly).

## Paper-наблюдение (Stage F, следующее)

- Запустить `python -m usstocks.bot` 10–20 сессий без сделок (только сигналы + ручные исходы через `/us_*`).
- Раз в день: `/us_export` → сверстать win rate / средний R / MAE.
- Параметры не менять без документированной причины.

## Архитектура

```
config/                профиль us_stocks_challenge.yaml + loader (профили)
usstocks/              новый пакет signal-only (только US-профиль его видит)
  models.py            Bar/TradeSignal/RiskState (tz-aware)
  indicators.py        VWAP 09:30-reset + OR15 из 3 закрытых 5m баров
  strategy/            vwap_pullback.py (чеклист 14 кодов, passed+failed)
  sizing.py            стоп-первый sizing $10/$5000
  risk_engine.py       6(+1) блокировок
  data/utex_provider.py  единый UTEX-фид (копирует HTTP-логику алертера)
  data/replay_provider.py CSV-replay
  session.py           Нью-Йоркский календарь/Holidays/DST
  premarket_ranker.py  §7.1 scoring → top-3 + Telegram-карточка
  notify.py            Notifier Protocol (реюз alerts.telegram_bot)
  scanner_loop.py      signal-only цикл + journal-хуки
  bot.py               один процесс: long-poll TG + сканы NY-сессии
  journal.py           SQLite us_* + export
  guards.py            assert_auto_trading_allowed (SystemExit 2)
execution/disabled_executor.py  единственный executor в signal-only профиле
alerts/us_commands.py  /us_* хендлеры с обязательным inline-подтверждением
```

Легаси (execution/mt5_trader.py, realtime/, challenge/manual/alerter.py сегодня, pairs) остаются и не задеты — их исполняющий код в signal-only ветку не импортируется.

## Тесты

```powershell
# Новые (89) + весь легаси:
venv\Scripts\python.exe -m pytest -q
# точечно (ТЗ §12):
venv\Scripts\python.exe -m pytest tests/test_vwap_* tests/test_sizing_and_risk_blocks.py tests/test_replay_offline.py tests/test_stage_d_provider_ranker_loop.py tests/test_stage_e_journal_and_commands.py tests/test_us_profile_guards.py tests/test_disabled_executor.py tests/test_us_profile_config.py -q
```

## Документация

- `docs/AUDIT.md` — снапшот существующей системы.
- `docs/MIGRATION_PLAN.md` — план поэтапной миграции B→F.
- `docs/STRATEGY_VWAP_PULLBACK.md` — спека стратегии с зафиксированными интерпретациями (OR-mid фильтр, уровни для 1.8R, роль ML).
