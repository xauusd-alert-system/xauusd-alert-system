# Отчёт об интеграции знаний из книг — TZ_BOOKS.md

**Дата:** 2026-08-28
**ТЗ:** `docs/analysis/TZ_BOOKS.md` (26 задач, P0→P3)
**Источники:** «Neural Networks for Algorithmic Trading with MQL5» + «MQL5
Programming for Traders» — оба анализа в `docs/analysis/`.

**Итог: 15 задач закрыто полностью, 10 сделано в коде с оговоркой о
контрольной сборке в MetaEditor, 1 отложена по условию самого ТЗ (T-24).
Регресс тестов не сломан: все падения, находимые в затронутых пакетах,
воспроизводятся на чистом дереве до интеграции.**

---

## 1. Закрыто полностью (код + тесты зелёные)

| Задача | Что сделано | Где |
|---|---|---|
| **T-01** Подключить NeuroBook | Инвентарь и зеркало исходников книги: `NEUROBOOK_MANIFEST.json` (53 файла CodeBase), `fetch_neurobook.py` для побайтового скачивания, зафиксирован коммит vendor `67efcaf0` | `mql5/NeuroBook/` |
| **T-02** Генератор выборок XAUUSD | `build_book_features` (RSI/MACD/геометрия), `NormalizationParams` (fit только на train, save/load JSON), `make_windowed_samples`, 60/20/20 по времени, `multi_horizon` [6,12]; CLI-раннер артефактов для EA | `model/sample_generator.py`, `scripts/create_initial_data_xauusd.py` |
| **T-03** Регламент валидации | `forward_metrics` + `evaluate_model_acceptance` (PF>1.2, WR>55%, ≥30 трейдов, порог ≥0.6, форвард ≥365д, замороженные параметры) + чек-лист | `backtest/validation_protocol.py` |
| **T-04** Базовая FC-модель | FC(60, Swish)+Adam по книге; градиент-чек 2.5e-8; smoke-обучение 0.38→0.0001 | `model/book_nn/`, `scripts/run_book_experiments.py` |
| **T-08** Тестер-контур | Python-близнец формулы `PF·√trades − w·DD%` (cap 10, 0 трейдов → −inf, equity-DD из кривой) + MQL5 `OnTester` с `FrameAdd("equity")` | `backtest/tester_criterion.py`, `mql5/NeuroTrader/TesterCriterion.mqh` |
| **T-09** MH Attention | MHA (8 голов, window_out=8, model_dim 32) в общем рантайме; smoke-сравнение fc/lstm/mha на синтетике END-TO-END (MSE 1.078/1.067/1.102 — ожидаемо ≈1 на random walk) | `scripts/run_book_experiments.py` |
| **T-10** Порог + day-of-week | `passes_trade_level` (0.6), `day_of_week_stats`, `blocked_days_from_stats` (блок только при WR<45% **И** PnL<0 **И** ≥30 трейдов; fail-open); MQL5-зеркало `DayFilter.mqh` | `model/day_of_week_filter.py`, `mql5/NeuroTrader/DayFilter.mqh` |
| **T-11** Gradient check в CI | Автотесты всех стеков (FC/LSTM/MHA/GPT/композит) + тест, что чекер ЛОВИТ сломанный backward (саботаж ×2 — тот самый класс бага /scale) | `model/book_nn/tests/test_gradient_check_ci.py` |
| **T-15** Новостной фильтр | Live: `NewsGuard.mqh` (календарь, ±30 мин, high-importance USD, merge окон, в тестере fail-open с пояснением). Бэктест: `NewsStore` (WAL, UNIQUE(ts,title,country), `events_between`, `import_csv`, `is_news_blackout`) | `mql5/NeuroTrader/NewsGuard.mqh`, `data/news_sqlite.py` |
| **T-16** Python-мост | `SignalBridgeWriter`: таблица `ml_signals` + `bridge_meta(schema_version=1)`, WAL, идемпотентная запись (retry не сбрасывает статус EA), TTL 3ч, `expire_stale`; MQL5-читатель с fail-closed по версии схемы; автоторговля Python не используется (10027 — фича) | `execution/signal_bridge.py`, `mql5/NeuroTrader/SignalBridge.mqh` |
| **T-17** Train/Val-расхождение | Мониторинг в цикле обучения: patience/min_train_progress/val_worsen_ratio, алерт с эпохой и обеими кривыми | `model/book_nn/train.py` |
| **T-19** Расширение фичесета | `atr_ratio`, `session_vol_ratio`, `volume_ratio` — масштабно-свободные, по схеме нормализации T-02 (`--extended-features`) | `model/sample_generator.py` |
| **T-20** Конфигурация моделей | `BookNetwork` из CLayerDescription-диктов, сериализация `.npz`+`.json` (архитектура как данные), `book_fc/lstm/mha_description` | `model/book_nn/network.py` |
| **T-23** Walk-forward + дрейф | `psi` (0.10/0.25), `ks_statistic` (scipy+exact fallback), `feature_drift_report`, `normalization_shift` (scale ratio >1.5 или mean shift >1σ → alarm), `walk_forward_drift_gate` (блокирует deploy); сам walk-forward-конвейер уже был в репо (`backtest/walk_forward.py`, `scripts/retrain_real_trades.py`) — интеграция дрейфа добавлена | `model/drift.py` |
| **T-25** Ансамбль | FC+LSTM+MHA голосование: `model_probability` (sigmoid/softmax), `ensemble_vote` (порог 0.6, min_agreement → «flat» при неустойчивости), `signal_stability` | `model/book_nn/ensemble_vote.py` |

## 2. Сделано (код написан; требуется контрольная сборка в MetaEditor)

В песочнице нет MetaEditor — MQL5-код написан по API, проверен балансом
скобок и контрактными тестами по исходникам, но **не компилировался**.

| Задача | Что сделано | Где |
|---|---|---|
| **T-05** Исполнение | OrderCheck→OrderSend/Async→классификация retcode (retryable: REQUOTE/PRICE_CHANGED/PRICE_OFF/TIMEOUT/CONNECTION/SERVER_BUSY/LOCKED), ретраи с refresh цены, filling из SYMBOL_FILLING_MODE, ConfirmAsync | `mql5/NeuroTrader/TradeExecutor.mqh` |
| **T-06** Риск-калькулятор | Лот из % equity через TICK_VALUE/SIZE, округление ВНИЗ к VOLUME_STEP, skip при min-lot>бюджета (никогда вверх), OrderCalcMargin-предпроверка | `mql5/NeuroTrader/RiskSizer.mqh` |
| **T-07** Алерты | SendNotification/SendMail/WebRequest-JSON-хук, JSON-escape, напоминание про whitelist URL | `mql5/NeuroTrader/AlertDispatcher.mqh` |
| **T-12** Money management | SL/TP со сдвигом на спред XAUUSD + валидация по TRADE_STOPS_LEVEL, трейлинг (только ужесточение), частичное закрытие на TP1 + BE по факту входа | `mql5/NeuroTrader/PositionManager.mqh` |
| **T-13** Событийная модель | Spy-индикатор: OnCalculate/OnTimer/OnBookEvent → CSV в MQL5\Files (три канала событий вместе) | `mql5/NeuroTrader/Indicators/EventTickSpy.mq5` |
| **T-14** Фичи через хэндлы | iRSI/iMACD в OnInit, gate BarsCalculated, CopyBuffer со start=1 (закрытый бар), 7 фич в порядке Python, окно `BuildNormalizedWindow` для FC, нормализация из JSON T-02 (fail-closed) | `mql5/NeuroTrader/FeatureEngine.mqh` |
| **T-18** OpenCL | Контекст/очередь/программа один раз в OnInit (главный урок книги 5.4), персистентные буферы, кернел `mlp_forward` (Swish+sigmoid), обязательный CPU-fallback; скрипт-бенчмарк GPU vs CPU; экспортёр весов FC → плоский .bin | `mql5/NeuroTrader/OpenCLInference.mqh`, `Scripts/BenchmarkOpenCL.mq5`, `scripts/export_fc_weights.py` |
| **T-21** MQL5-проект | `NeuroTrader.mproj`: 4 программы (EA, spy, 2 скрипта) + 11 разделяемых .mqh | `mql5/NeuroTrader/NeuroTrader.mproj` |
| **T-22** Аудит сигналов | `signal_trace`: фичи(hash+json)→решение→алерты→исполнение(retcode, tickets)→результат; INSERT OR IGNORE идемпотентность | `mql5/NeuroTrader/SignalJournal.mqh` |
| **T-26** Кастомные символы | `XAUUSD_NV`: Wilder-ATR(14)/close ×100 как бары кастомного символа; агрегация в нестандартный ТФ (напр. 180 мин) | `mql5/NeuroTrader/Scripts/CreateVolatilitySymbol.mq5` |

Сборка: открыть `mql5/NeuroTrader/NeuroTrader.mproj` в MetaEditor → F7.

## 3. Заблокировано / отложено

| Задача | Причина |
|---|---|
| **T-24** GPT-блоки | Отложено **по условию самого ТЗ**: «после стабилизации walk-forward и при наличии GPU». Слой `GPTStyleBlock` написан и проверен градиент-чеком (4.73e-8) — блокировки на уровне библиотеки нет, не запускается только применение. |
| **Компиляция MQL5** | В Linux-песочнице нет MetaEditor. Синтаксис выверен по документации API (в процессе исправлены несуществующие `DatabaseReadExecute`/`DatabaseChanges`/`StringTrim` — заменены на документированные эквиваленты), баланс скобок проверен. Требуется одна контрольная сборка в терминале. |
| **Эксперименты на реальных данных** | `data/market_data_mt5.sqlite` в песочнице пуст — реальных свечей XAUUSD M5 нет. Полный прогон `run_book_experiments` выполнен на синтетике 20000 баров (MSE≈1 = честное «нет edge» на random walk). Для реального прогона: заполнить БД и `python -m scripts.run_book_experiments --asset XAUUSD --timeframe M5`. |
| **Замер ускорения OpenCL** | Нужен терминал с GPU-драйвером; скрипт `BenchmarkOpenCL.mq5` готов и пишет CSV. |

## 4. Верификация

**Новые тесты: 84 passed** (`pytest` по книгам-интеграции):

```
model/tests/test_books_sample_generator.py        9   (T-02/T-19)
model/tests/test_books_day_filter_drift.py        11  (T-10/T-23)
model/book_nn/tests/test_gradient_check_ci.py     6   (T-11)
model/book_nn/tests/test_train_config.py          5   (T-17/T-20)
backtest/tests/test_books_protocol_criterion.py   14  (T-03/T-08)
data/tests/test_books_news_sqlite.py              5   (T-15)
execution/tests/test_books_signal_bridge.py       9   (T-16)
scripts/tests/test_books_artifacts.py             9   (T-25 + скрипты)
mql5/tests/test_neurotrader_contracts.py          11  (MQL5↔Python контракты)
config/tests/test_books_config.py                 5   (секция books:)
```

Градиентные проверки (`model/book_nn`): FC 2.5e-8, LSTM 6.4e-8, GPT
4.73e-8, MHA ≤3.66e-5 (6 seed'ов, tol 1e-4), композит MHA+LSTM+FC 1.06e-5.

**Регресс:** полный прогон затронутых пакетов — 798 passed / 10 failed;
все 10 падений (`execution/test_close_notification*`, `test_breakeven_legs*`,
`test_blackout*`, `scripts/test_audit_final_batch*`,
`scripts/test_diag_r_metrics*`) воспроизводятся на чистом дереве до
интеграции (проверено git-stash) — это существующие проблемы
`MultiAssetMT5Trader` (нет `trade_throttle`/`strategy_identity` в тестовом
пути инициализации), не связанные с задачами книг.

Контрактные тесты MQL5↔Python фиксируют: порядок 7 фич
(`FeatureEngine` == `FEATURE_COLUMNS_BASE`), колонки/статусы/версию схемы
моста, join-ключ `features_hash`, формулу критерия, отказ при
несовпадении schema_version.

## 5. Как это запускать

```bash
# датасет + артефакты для EA (нормализация, day filter, samples.npz)
python -m scripts.create_initial_data_xauusd --out-dir data/book_initial

# эксперименты FC/LSTM/MHA (реальные данные, когда sqlite заполнен)
python -m scripts.run_book_experiments --asset XAUUSD --timeframe M5

# экспорт обученной FC для EDGE-режима EA (OpenCL/CPU)
python -m scripts.export_fc_weights --model output/book_experiments/book_fc \
    --out book_fc_weights.bin

# тесты
python -m pytest model/tests model/book_nn backtest/tests data/tests \
    execution/tests scripts/tests mql5/tests config/tests
```

EA: `mql5/NeuroTrader/NeuroTraderEA.mq5`, режимы BRIDGE (Python-сигналы
через `ml_signal_bridge.sqlite`) и EDGE (локальный инференс). Артефакты
для `MQL5\Files`: `book_normalization.json`, `book_day_filter.json`,
`book_fc_weights.bin` — см. `mql5/NeuroTrader/README.md`.

## 6. Конфигурация

Секция `books:` в `config/config.yaml` (samples/trade_level/day_filter/
news_guard/validation/tester_criterion/bridge/drift) + accessor
`config.loader.books_config()` с дефолтами и отказом на неизвестные ключи.
Существующие секции не тронуты.
