# Migration Plan — us_stocks_challenge (signal-only VWAP Pullback)

База: `docs/AUDIT.md`. Принципы ТЗ §4: минимальные обратимые изменения, ничего не удаляем,
легаси (MT5/пары/дашборд) живёт дальше в своих профилях. Целевая структура из ТЗ §5
адаптирована под существующий layout: новые модули — в новый пакет `usstocks/`,
переиспользуем `alerts/telegram_bot.py`, `challenge/manual/` примитивы и `config/loader.py`.

---

## Этап B — безопасность и профили

Цель: автоторговля технически невозможна в профиле `us_stocks_challenge`.

1. **Ветка** `feature/us-stocks-vwap-scanner` от текущего состояния (после фиксации грязного worktree отдельным коммитом владельцем).
2. **Профили конфига** (`config/loader.py`, минимальная правка):
   - `PROFILE` env / аргумент `--profile`; значения: `us_stocks_challenge` (новый), `replay`, `forex_legacy` (дефолт = текущее поведение), `crypto_legacy`.
   - Легаси-старт без профиля работает как сегодня.
3. **Новый файл `execution/disabled_executor.py`**:
   - `Executor` Protocol (`submit(order)`) + `DisabledExecutor`: логирует WARNING с полным order-dict и бросает `ExecutionDisabledError`.
   - Фабрика `build_executor(profile)` → DisabledExecutor для `us_stocks_challenge|replay`.
4. **Startup-interlocks** (3 точки):
   - `scripts/run_bot.py` main(): если profile=us_stocks_challenge → exit(2) c понятным сообщением;
   - `challenge/runner.py` main(): то же;
   - `challenge/stealth/runner_bridge.build_engine()`: то же.
   - Общий хелпер `usstocks/guards.assert_no_execution(profile)` чтобы не дублировать.
5. **Конфиг челленджа `config/us_stocks_challenge.yaml`**: числа из ТЗ §2 verbatim (account 1000, daily −50/total −100 official; внутренние: $10/trade, −$20 personal stop, max 2 сделки, 2 подряд лосса, +$20 profit lock, 25 min до закрытия запрет входов), scanner-фильтры §7.1, strategy-параметры §7.5, `model.enabled: false / role: advisory_only`, `execution.mode: disabled`, watchlist-universe + SPY/QQQ.
6. **Гигиена тестов**: переименовать `scripts/test_crypto_regime_aug24.py` → `scripts/diag_crypto_regime_aug24.py` (убирает живой network-call из коллекции pytest). Логика не меняется.

Файлы этапа:
- изменяем: `config/loader.py`, `scripts/run_bot.py`, `challenge/runner.py`, `challenge/stealth/runner_bridge.py`, `pyproject.toml` (если решим через testpaths-ignore вместо переименования), `requirements.txt` (+playwright pin)
- создаём: `config/us_stocks_challenge.yaml`, `execution/disabled_executor.py`, `usstocks/__init__.py`, `usstocks/guards.py`, `tests/test_us_profile_guards.py`, `tests/test_disabled_executor.py`

Тесты: interlock-тесты (каждая точка старта отказывается работать в us-профиле), DisabledExecutor raising, легаси-профиль не затронут (существующие 1286 тестов зелёные).

## Этап C — replay baseline (стратегия + риск на CSV)

Доменные модели и чистые функции без сети:

1. `usstocks/models.py` — `Bar`, `MarketEvent`, `PremarketSnapshot`, `TradeSignal`, `RiskState`, `RiskEvent`, `OrderRequest` (dataclasses/pydantic, tz-aware).
2. `usstocks/indicators.py` — `vwap(bars)`, `opening_range(bars_5m, minutes=15)` (3 закрытые 5m-свечи), helpers volume-ratio/higher-low/lower-high. Только по закрытым барам.
3. `usstocks/strategy/vwap_pullback.py` — long/short правила ТЗ §7.3–7.4 как явный checklist: функция возвращает `(signal | None, passed[], failed[] reasons)` — все причины фильтрации сохраняются в журнал.
4. `usstocks/sizing.py` — порядок «stop → shares» из ТЗ §8 (`floor($10/risk_per_share)`, cap `$5000/entry`), проверка `shares>0/notional≤5000/risk≤10`.
5. `usstocks/risk_engine.py` — блокировки ТЗ §8 (PERSONAL_DAILY_STOP, MAX_TRADES_REACHED, MAX_CONSECUTIVE_LOSSES, DAILY_PROFIT_LOCK, SESSION_CLOSE_GUARD, ACTIVE_POSITION_EXISTS) поверх дневного RiskState; троттлинг повторных Telegram-отказов.
6. `usstocks/data/replay_provider.py` — чтение CSV 1m/5m fixtures, воспроизведение сессии, ноль сетевых вызовов.
7. `usstocks/replay.py` — CLI `python -m usstocks.replay --csv … --date …` → печатает watchlist/сигналы/отказы/risk-events.
8. Fixtures: синтетические OHLCV-сценарии (валидный long, invalid close-below-VWAP, валидный short, QQQ-conflict, gap/vol фильтры).

Файлы этапа: `usstocks/models.py`, `indicators.py`, `strategy/vwap_pullback.py`, `sizing.py`, `risk_engine.py`, `data/replay_provider.py`, `replay.py`, `tests/fixtures/*.csv`, `tests/test_vwap_indicators.py`, `test_vwap_long_short.py`, `test_benchmark_conflict.py`, `test_sizing_caps.py`, `test_risk_blocks.py`, `test_replay_offline.py`.

Тесты: пункты 2–13, 15 чеклиста ТЗ §12.

## Этап D — watchlist и live data adapter

1. `usstocks/data/utex_provider.py` — реализация `MarketDataProvider` поверх **существующего** UTEX-фетча `challenge/manual/alerter.py` (вынести fetch/refresh-token в переиспользуемый модуль, alerter переключить на него — поведение не меняется). 1m бары, premarket snapshot, SPY/QQQ.
2. `usstocks/premarket_ranker.py` — скоринг ТЗ §7.1 (gap/rel-vol/news/dollar-vol/spread) → top-3 watchlist + Telegram-рассылка до открытия.
3. `usstocks/session.py` — NYSE календарь `America/New_York` (zoneinfo, DST-correct; выходные/праздники — список из `challenge/stealth/session_simulator._US_MARKET_HOLIDAYS_2026` переносится в конфиг, не импортируется из stealth).
4. `usstocks/scanner_loop.py` — signal-only раннер: poll → strategy → risk → notifier → journal. Никаких ордеров; Executor отсутствует в графе вовсе (не только Disabled).

Тесты: ranker на фиксированных snapshot'ах, provider на записанных JSON-ответах (без сети), session-границы вокруг DST.

## Этап E — Telegram и журнал

1. `alerts/us_commands.py` — регистрируется в **существующем** control-bot: `/us_status /us_watchlist /us_signals on|off /us_pnl /us_win /us_loss /us_flat /us_stop /us_resume /us_export`. Изменения PnL/статуса — только после inline-подтверждения (callback-кнопка ✅/❌). MT5-команды остаются как есть, но в us-профиле не регистрируются.
2. Формат сигнала — шаблон ТЗ §9 (entry/stop/risk/share/notional/TP1/TP2/почему/дисклеймер signal-only) + кнопки `Принял/Отклонил/Детали/Стоп на день`.
3. `usstocks/journal.py` — SQLite `data/usstocks.sqlite`: `us_sessions, us_watchlist_snapshots, us_signals, us_trade_outcomes, us_risk_events` (+ source/strategy-version/все метрики момента сигнала/решение пользователя); `/us_export` → CSV за день.
4. Ручной ввод PnL `/us_pnl 12.5` → кнопка подтверждения → запись исхода и пересчёт RiskState.

Тесты: пункт 16 ТЗ §12 (PnL меняется только после подтверждения), journal round-trip, формат сообщения содержит обязательные поля.

## Этап F — paper-наблюдение (после вашего прогона)

Инструкции в README: запуск `--profile us_stocks_challenge`, 10–20 сессий, экспорт CSV, метрики win rate/avg R/MAE. Параметры не трогаем без документированной причины.

---

## Сводный список файлов

### Создаваемые
```
docs/AUDIT.md                                   (готово)
docs/MIGRATION_PLAN.md                          (этот файл)
docs/STRATEGY_VWAP_PULLBACK.md                  (этап C — спека стратегии)
config/us_stocks_challenge.yaml                 (B)
execution/disabled_executor.py                  (B)
usstocks/__init__.py                            (B)
usstocks/guards.py                              (B)
usstocks/models.py                              (C)
usstocks/indicators.py                          (C)
usstocks/sizing.py                              (C)
usstocks/risk_engine.py                         (C)
usstocks/strategy/__init__.py                   (C)
usstocks/strategy/vwap_pullback.py              (C)
usstocks/data/__init__.py                       (C/D)
usstocks/data/replay_provider.py                (C)
usstocks/data/utex_provider.py                  (D)
usstocks/premarket_ranker.py                    (D)
usstocks/session.py                             (D)
usstocks/scanner_loop.py                        (D)
usstocks/journal.py                             (E)
alerts/us_commands.py                           (E)
tests/test_us_profile_guards.py                 (B)
tests/test_disabled_executor.py                 (B)
tests/test_vwap_indicators.py                   (C)
tests/test_vwap_signals.py                      (C)
tests/test_sizing_and_risk_blocks.py            (C)
tests/test_replay_offline.py                    (C)
tests/fixtures/*.csv                            (C)
tests/test_us_journal_and_commands.py           (E)
```

### Изменяемые (минимальные диффы)
```
config/loader.py                      — profile-резолюция (~30 строк, дефолт = текущее поведение)
scripts/run_bot.py                    — interlock при us-профиле (guard в main())
challenge/runner.py                   — interlock при us-профиле
challenge/stealth/runner_bridge.py    — interlock при us-профиле
challenge/manual/alerter.py           — ROOT из __file__ вместо хардкода; fetch вынесен в usstocks.data.utex_provider (D)
challenge/manual/quality_gate.py      — фикс дубля строк 79–83 (гигиена, отдельным коммитом)
requirements.txt                      — +playwright (объявить существующую зависимость)
README.md                             — установка/replay/live signal-only (финал)
scripts/test_crypto_regime_aug24.py   — переименование в diag_* (без правки логики)
```

### Не трогаем (легаси продолжает работать)
`execution/mt5_trader.py`, execution-адаптеры, `alerts/control_bot.py` (MT5-часть),
`realtime/*`, `model/*`, `backtest/*`, `pairs_analysis/*`, `simulation/*`,
`challenge/stealth/*` (код остаётся, но не вызывается us-профилем).

## Breaking changes
- Нет для легаси: без переменной PROFILE поведение идентично текущему.
- Внутри us-профиля исполнение невозможно архитектурно (DisabledExecutor + отсутствие executor в графе + 3 startup-interlock'а).

## Критерии готовности
Чеклист ТЗ §14; приёмочный прогон: полный pytest (1286+ новых) зелёный,
`python -m usstocks.replay` на fixture воспроизводит сессию без сети,
`--profile us_stocks_challenge` поднимает сканер/TG и отказывается стартовать любой исполняющий модуль.
