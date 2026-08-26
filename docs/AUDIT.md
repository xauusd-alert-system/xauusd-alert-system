# Current System Audit

Дата аудита: 2026-08-26. Ветка: `prop-challenge` @ `1156739` (синхронизирована с origin).
Рабочее дерево грязное: 18 изменённых файлов + ~30 untracked (см. §9).

---

## Запуск

| Точка входа | Команда | Что делает |
|---|---|---|
| MT5 трейдер (легаси) | `python -m scripts.run_bot` | Виртуальная симуляция + Telegram control bot + `MultiAssetMT5Trader.run_loop()` |
| Дашборд | `uvicorn realtime.app:app` (FastAPI) | Web UI, WS `/ws/dashboard`, `/api/*` только чтение |
| US-stocks алертер (signal-only) | `venv\Scripts\python.exe challenge\manual\alerter.py [--once|--test]` | Поллит UTEX API, сканирует сетапы, шлёт Telegram. **Ордера не отправляет** |
| Browser-бот челленджа | `python -m challenge.runner` | Playwright открывает терминал HashHedge и **кликает ордера сам** |
| Stealth-слой | `challenge/stealth/runner_bridge.py` (вызывается из runner) | Hotkey-исполнение F1/F2… через браузер + «гуманизация» эквити |
| Обучение/бэктесты | `scripts/*.py` (82 файла), `python -m scripts.train_mt5 …`, overnight pipeline | Легаси forex/metals/crypto |

- Python 3.13, venv в `venv/`. Зависимости: `requirements.txt` (fastapi, pandas 3.0, xgboost, scikit-learn, python-telegram-bot 22.8, requests, MetaTrader5 — Windows-marker).
- **Playwright используется (`challenge/browser.py`, `challenge/manual/alerter.py`), но в requirements.txt ОТСУТСТВУЕТ** — неявная зависимость.
- pytest: `pyproject.toml` → `pythonpath = [".", "simulation/mt5_shim"]`. Коллекция: **1286 тестов, 1 ошибка коллекции**.

## Текущая архитектура

| Модуль | Назначение | Вердикт | Причина |
|---|---|---|---|
| `config/` (config.yaml, loader.py, .env через dotenv) | Конфиг + секреты | **оставить / адаптировать** | Добавить профили (`us_stocks_challenge`, replay); loader расширяется минимально |
| `data/` (MT5→SQLite, trading_event_ledger, intent_ledger) | Рыночные данные + журналы исполнения | **оставить** (легаси) | Не нужен US-профилю, но работает — не трогаем |
| `features/`, `labeling/`, `model/`, `regime/` | XGBoost direction-модели + ансамбль-гейты | **оставить (легаси)** | В us-профиле ML = advisory_only, права разрешать сделку нет |
| `backtest/`, `pairs_analysis/`, `scripts/` | Исследования, пары, CLI | **оставить (легаси)** | Не мешают |
| `execution/mt5_trader.py` + adapters (`broker_adapter`, `mt5_hedging/netting`) | **Автоисполнение MT5** (`order_send` ×3+ в trader, адаптеры, fx_execution_probe) | **отключить в us-профиле** | Жёсткий interlock при старте; код не удаляем |
| `execution/risk_manager.py`, `trade_throttle.py` | Риск-гейты MT5-аккаунта | **оставить (легаси)** | ⚠ Незакоммиченное состояние: лосс-гейты отключены по запросу владельца — фиксировать как осознанное решение |
| `alerts/telegram_bot.py` | Отправка сигналов (токен из env, redaction) | **оставить, переиспользовать** | Готовый Notifier для US-сигналов |
| `alerts/control_bot.py` | Управляющий бот; `/closeall` → `mt5.order_send` (строка 645) | **адаптировать** | MT5-команды остаются в легаси-профиле; `/us_*` команды добавляются отдельно |
| `alerts/status_commands.py`, `challenge_commands.py` | Read-only статусы (тест гарантирует отсутствие order_send) | **оставить** | Образец honesty-контракта |
| `realtime/` (app.py, dashboard.py, book_feed) | Дашборд + DOM-feed | **оставить (легаси)** | К US-профилю не подключается |
| `challenge/manual/scanner.py` | Rule-based сканер US-акций: impulse-pullback, gap_fade, opening_drive на 1m-свечах UTEX | **переиспользовать** | Ядро будущего сканера; добавить VWAP/OR15m как новый setup-type рядом |
| `challenge/manual/alerter.py` | Signal-only цикл: poll UTEX → quality-gate → Telegram; журнал CSV/JSON | **переиспользовать** | Уже реализует требуемый режим; ROOT захардкожен (строка 27) — починить |
| `challenge/manual/risk.py` | Профили C/B/A, дневной state-machine ($5 риск, стоп-день после лоссов) | **адаптировать** | Числа привести к внутренним лимитам ТЗ (§2): $10/trade, -$20 daily stop, max 2 сделки |
| `challenge/manual/outcomes.py`, journal | Журнал исходов + per-symbol статистика | **переиспользовать** | Основа `us_trade_outcomes` |
| `challenge/manual/crypto_regime.py`, quality_gate/autocal/score_live | Фильтры качества | **оставить** | Баг в `quality_gate.py:79-83` (дубль строки + мусорный return) — починить отдельным коммитом |
| `challenge/connector.py` | **Playwright-ордера** (place_order/close_position/modify_stop кликами) | **отключить в us-профиле** | Изъять из цепочки вызовов; код сохраняется |
| `challenge/browser.py`, `login.py`, `runner.py` | Браузерная автоторговля ORB | **отключить в us-профиле** | Interlock при старте профиля |
| `challenge/stealth/*` (7 модулей, untracked) | Имитация ручных действий: hotkeys, humanizer эквити/тайминга | **изолировать** | Прямое противоречие ТЗ §15 («не имитировать ручные действия»). Не вызывается новым профилем; решение об удалении — только с подтверждения владельца |
| `challenge/orb_strategy.py`, `strategy.py`, `windows.py`, backtests | ORB-логика и исследования | **оставить** | OR15m-утилиты пригодятся VWAP-стратегии |
| `simulation/` (shim MT5, virtual_state) | Симуляция без реального терминала | **оставить** | Пример изоляции исполнения — образец для DisabledExecutor |

## Потоки данных

1. **MT5/FxPro demo** → M5 бары (XAU/XAG/BTC/EUR/GBP) → SQLite `data/market_data_mt5.sqlite`. Таймфреймы M5/M15/H1. Только легаси.
2. **UTEX REST/gRPC** (`api.utex.io` refresh-token + `demoususdt-api-margin.utex.io` grpc) → 1m свечи US-акций (18–22 тикера watchlist). Токены в `data/challenge_tokens.json` (gitignored). Фетч через Playwright request-context (обход SSL-проблем РФ-сети). Это источник для us-профиля.
3. **MT5 book/DOM** (`realtime/book_feed.py`) — только BTC реально отдаёт стакан; легаси.
4. Новости/сентимент (`news/`, `data/sentiment_analyzer.py`) — опциональные гейты легаси.

Таймзоны сейчас смешанные: manual-система живёт в UTC (сессия 13:30–19:55 UTC ≈ 09:30–15:55 ET), конфиг-комментарии упоминают UTC+4/+5 локали. **Для us-профиля обязателен timezone-aware datetime и торговый календарь `America/New_York`** (сейчас tz-хелперы только в `challenge/windows.py`, ET захардкожен как фиксированный −4 без DST).

## Стратегии и модели

- **Легаси ML**: пер-ассет XGBoost (2-class/3-class), blended confidence (rule 0.10–0.20 / ml 0.80–0.90), гейты min_confidence/EV/bifurcation. Модель **авторитетна** в MT5-исполнении.
- **Manual scanner** (US): impulse→pullback + gap_fade + opening_drive; quality score 0–100 (volume/tod/regime) с калиброванными порогами; журнал R-исходов. Risk: look-ahead нет (сканер потребляет только закрытые бары), но:
  - калибровка порогов сделана на big-cap выборке, live-watchlist — мелкая крипта/майнеры → уже привело к паузе gap_fade (коммент в manual_config.yaml);
  - `setup_outcomes.csv` содержит искажённые цены (ACHR entry 623 000 000 — единицы UTEX, а не USD) → расчёт R корректен относительный, но абсолютные уровни нельзя сравнивать с долларовыми;
  - `quality_gate.compute_quality_for_setup` сломан незакоммиченным дублем строк (возвращает dict вместо int местами).
- **VWAP Pullback Continuation отсутствует** (есть только ORB-варианты в `challenge/backtest/vwap_opening_drive.py` — исследовательские).

## Исполнение (все точки, где может родиться ордер)

| # | Где | Механизм | Статус в us-профиле |
|---|---|---|---|
| 1 | `execution/mt5_trader.py:1338,1392,1653`; `broker_adapter.py:214,249,268`; `mt5_hedging/netting_adapter.py`; `fx_execution_probe.py` | MT5 `order_send` | Interlock: отказ старта при profile=us_stocks_challenge |
| 2 | `alerts/control_bot.py:645` (`/closeall`) | MT5 `order_send` | MT5-команда остаётся легаси; us-бот её не регистрирует |
| 3 | `challenge/connector.py` place_order/close_position/modify_stop | Playwright-клики по DOM терминала | Не импортируется us-раннером |
| 4 | `challenge/runner.py` + `challenge/stealth/runner_bridge.execute_plan/execute_actions` (+ hotkeys `browser_hotkey_map`) | Автоклики/hotkeys в терминале | Interlock при старте; stealth не вызывается |
| 5 | `logs/ws_live_test.py` | Разовый live-скрипт order_send | Вне пакетов, в git не входит; пометить в README как небезопасный |

Гарантия signal-only: новый профиль собирается ТОЛЬКО из (data provider → strategy → risk engine → notifier → journal). Единственный Executor в графе — `DisabledExecutor`, который логирует warning и бросает `ExecutionDisabledError`. Плюс тест №14 из ТЗ: обработка сигнала никогда не вызывает `Executor.submit()`.

## Риски и несоответствия условиям Challenge

1. **Незакоммиченный конфиг ослабил все лосс-гейты MT5** (`risk_throttle`: hard_stop/cooldown/daily-loss = off; `max_daily_trades_per_asset` 15→100; `execution.volume` 1 лот × 3 ноги). Для легаси-MT5 это осознанное решение владельца, но оно не должно протекать в us-профиль — у того свой risk-engine с числами ТЗ.
2. **Browser automation + stealth** существуют и запускаются — противоречат запретам ТЗ §15, если их использовать. По умолчанию изолируются interlock'ом; удаление — отдельное решение.
3. Watchlist-качествo: текущие тикеры (CAN, BTBT, HIVE…) — низкий номинал/спред-риск против фильтров ТЗ §7.1 (min_price $10, rel-vol ≥1.5, dollar-vol ≥$50M). Новый premarket-ranker считает юниверс заново.
4. Смешанная таймзонная математика (UTC vs локаль UTC+4/+5) — источник ошибок границ сессии; в us-коде всё в aware-datetime + America/New_York.
5. `day_state.json` завис на 2026-08-21 — дневной state-machine не обновляется этим раннером; в us-профиле состояние риска пересчитывается на каждом решении, файл — только зеркало.
6. Секреты: `.env` и токены UTEX не закоммичены ✓; `.env.example` есть ✓. Найденное исключение — хардкод абсолютного пути `ROOT` в alerter.py:27 (не секрет, но ломает портируемость).
7. Тест-гигиена: `scripts/test_crypto_regime_aug24.py` (untracked) делает **живой сетевой запрос при импорте** — единственная ошибка коллекции pytest. Переименовать/перенести вне testpaths.
8. Playwright не объявлен в requirements.txt.

## Незакоммиченное состояние (важно до любых правок)

18 изменённых файлов (~1234+/329−): рефакторинг connector, crypto-regime фильтр, live-калибровка quality, дашборд WS, отключение MT5-гейтов, `gap_fade_enabled: false`, `opening_drive: 60→0`.
~30 untracked: `challenge/stealth/`, `crypto_regime.py`, `quality_score_live.py`, orb_strategy, тесты stealth, инструменты DOM.
**Перед ветвлением рекомендуется зафиксировать текущее состояние отдельным коммитом (или стешем) — иначе миграция смешается с незавершённой работой.**

## План миграции

См. `docs/MIGRATION_PLAN.md`. Кратко:

- **A (этот этап)**: аудит + план. Без изменения бизнес-логики.
- **B**: ветка `feature/us-stocks-vwap-scanner`; профили конфига; `DisabledExecutor`; startup-interlocks для MT5-trader/browser-runner/stealth; тест-гвардии.
- **C**: доменные модели Bar/Signal/RiskState; индикаторы VWAP + Opening Range 15m; стратегия VWAP Pullback (long/short); position sizing ($10 риск / $5000 номинал); risk engine с блокировками ТЗ §8; replay CSV; fixtures + тесты §12.
- **D**: US data provider (UTEX-адаптер поверх существующего фетча alerter'а); premarket ranker top-3; SPY/QQQ benchmark; NY-сессия/tz.
- **E**: `/us_*` команды и inline-подтверждения в существующем Telegram; SQLite-журнал `us_*` + CSV export.
- **F**: paper-наблюдение 10–20 сессий, метрики, экспорт.

Breaking changes: для легаси-профилей — никаких (новый код аддитивен; interlocks срабатывают только при явном выборе us-профиля).
