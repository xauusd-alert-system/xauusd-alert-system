# NeuroTrader — MQL5- сторона интеграции книг (TZ_BOOKS)

MQL5-реализация задач из `docs/analysis/TZ_BOOKS.md`: исполнение,
риск, алерты, фильтры, локальный инференс и мост к Python-пайплайну.
Откройте `NeuroTrader.mproj` в MetaEditor и соберите проект (F7).

## Состав

| Файл | Задача ТЗ | Что делает |
|---|---|---|
| `NeuroTraderEA.mq5` | сборка | EA: два режима — BRIDGE (Python-сигналы) и EDGE (локальный инференс) |
| `FeatureEngine.mqh` | T-14 | 7 фич книги через хэндлы iRSI/iMACD, gate `BarsCalculated`, нормализация из JSON T-02 (fail-closed) |
| `OpenCLInference.mqh` | T-18 | MLP-инференс на GPU (контекст создаётся один раз), CPU-fallback |
| `RiskSizer.mqh` | T-06 | лот из % equity, округление ВНИЗ к шагу, skip при min-lot > бюджета, `OrderCalcMargin` |
| `TradeExecutor.mqh` | T-05 | OrderCheck → OrderSend/Async, классификация retcode, ретраи |
| `AlertDispatcher.mqh` | T-07 | push / mail / WebRequest-JSON-хук (whitelist!) |
| `PositionManager.mqh` | T-12 | SL/TP с учётом спреда XAUUSD, трейлинг, частичное закрытие + BE |
| `NewsGuard.mqh` | T-15 | blackout ±N мин вокруг важных USD-событий (в тестере календарь недоступна → fail-open + SQLite-таблица для бэктеста) |
| `DayFilter.mqh` | T-10 | дни недели из собственной статистики (JSON от Python), fail-open |
| `SignalBridge.mqh` | T-16 | чтение таблицы `ml_signals` из SQLite (WAL), статусы new→consumed→executed/skipped/failed/expired |
| `SignalJournal.mqh` | T-22 | полная трасса сигнала в SQLite (фичи → решение → алерт → исполнение → результат) |
| `TesterCriterion.mqh` | T-08 | критерий OnTester `PF·√trades − w·DD%`, equity-фреймы |
| `Indicators/EventTickSpy.mq5` | T-13 | spy-индикатор: OnCalculate/OnTimer/OnBookEvent → CSV |
| `Scripts/BenchmarkOpenCL.mq5` | T-18 | замер GPU против CPU |
| `Scripts/CreateVolatilitySymbol.mq5` | T-26 | кастомный символ `XAUUSD_NV` (нормированная волатильность) |

## Режимы работы EA

**BRIDGE (по умолчанию).** Python обучает и считает вероятности,
записывает намерения в `MQL5\Files\ml_signal_bridge.sqlite`
(`execution/signal_bridge.py`). EA по таймеру (не в OnTick!) забирает
`new`-строки, проводит через ворота (день/новости/спред/порог),
маркирует `consumed`, исполняет и флипает в `executed/failed/skipped`.
Автоторговля из Python остаётся выключенной (ошибка 10027 — фича).

**EDGE.** EA сам считает фичи окна `InpWindow` закрытых баров,
прогоняет экспортированную FC-модель через OpenCL (или CPU-fallback)
и торгует сигму-вероятность против `InpMinProbability`.

Артефакты, которые кладут в `MQL5\Files`:

```
book_normalization.json   # scripts/create_initial_data_xauusd.py
book_day_filter.json      # там же (--trades-csv включает фильтр)
book_fc_weights.bin       # scripts/export_fc_weights.py
ml_signal_bridge.sqlite   # Python-писатель
```

## Проверка в тестере

- Критерий оптимизации: `OnTester` возвращает `PF·√trades − w·DD%`
  (T-08); equity-кривая уходит через `FrameAdd`.
- Финальный проход — «Каждый тик на основе реальных тиков».
- Календарь в тестере недоступна (4014): новостные окна для бэктеста
  берутся из SQLite-таблицы `data/news_sqlite.py`.

## Статус компиляции

Компилятор MetaEditor в окружении разработки недоступен: синтаксис
написан по API MQL5 и требует контрольной сборки в MetaEditor —
см. отчёт `docs/BOOKS_INTEGRATION_REPORT.md`.
