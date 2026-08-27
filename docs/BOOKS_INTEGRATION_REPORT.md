# Отчёт об интеграции знаний из книг — TZ_BOOKS.md

**Дата:** 2026-08-28
**ТЗ:** `docs/analysis/TZ_BOOKS.md` (26 задач, P0→P3)
**Источники:** «Neural Networks for Algorithmic Trading with MQL5» + «MQL5
Programming for Traders» — оба анализа в `docs/analysis/`.

**Итог: 15 задач закрыто полностью, 10 сделано в коде с оговоркой о
контрольной сборке в MetaEditor, 1 отложена по условию самого ТЗ (T-24).
Регресс тестов не сломан: все падения, находимые в затронутых пакетах,
воспроизводятся на чистом дереве до интеграции.**

**Дополнение (2026-08-28, вечер): эксперименты на РЕАЛЬНЫХ данных
разблокированы** — импортирован публичный датасет XAUUSD M15 2004–2025
(480 717 баров), полный цикл «датасет → обучение FC/LSTM/MHA → ансамбль →
сигнал в мост» прогнан на реальном рынке (раздел 3); T-16 доведён до
end-to-end: `scripts/publish_book_signals.py` (модели → `ensemble_vote` →
`SignalIntent` в `ml_signal_bridge.sqlite`).

**Дополнение 2: реальный рынок прогнан через ВСЕ оставшиеся Python-компоненты
книг** — дрейф-гейт T-23 (`run_book_drift_report.py`: PSI 0.318 → alarm,
деплой заблокирован), бэктест ансамбля с приёмочным вердиктом T-03/T-08
(`run_book_ensemble_backtest.py`: 65 сделок, PF 1.24, WR 41.5% → FAIL),
extended-фичи T-19 (edge не дали, но вскрыли и починили 2 бага фичесета,
которые синтетика не ловила). подробности — 3.4–3.6.

---

## 1. Закрыто полностью (код + тесты зелёные)

| Задача | Что сделано | Где |
|---|---|---|
| **T-01** Подключить NeuroBook | Инвентарь и зеркало исходников книги: `NEUROBOOK_MANIFEST.json` (53 файла CodeBase), `fetch_neurobook.py` для побайтового скачивания, зафиксирован коммит vendor `67efcaf0` | `mql5/NeuroBook/` |
| **T-02** Генератор выборок XAUUSD | `build_book_features` (RSI/MACD/геометрия), `NormalizationParams` (fit только на train, save/load JSON), `make_windowed_samples`, 60/20/20 по времени, `multi_horizon` [6,12]; CLI-раннер артефактов для EA | `model/sample_generator.py`, `scripts/create_initial_data_xauusd.py` |
| **T-03** Регламент валидации | `forward_metrics` + `evaluate_model_acceptance` (PF>1.2, WR>55%, ≥30 трейдов, порог ≥0.6, форвард ≥365д, замороженные параметры) + чек-лист; первый РЕАЛЬНЫЙ вердикт: FAIL для books-ансамбля (WR 41.5% ≤ 55%, п. 3.6) | `backtest/validation_protocol.py` |
| **T-04** Базовая FC-модель | FC(60, Swish)+Adam по книге; градиент-чек 2.5e-8; smoke-обучение 0.38→0.0001 | `model/book_nn/`, `scripts/run_book_experiments.py` |
| **T-08** Тестер-контур | Python-близнец формулы `PF·√trades − w·DD%` (cap 10, 0 трейдов → −inf, equity-DD из кривой) + MQL5 `OnTester` с `FrameAdd("equity")` | `backtest/tester_criterion.py`, `mql5/NeuroTrader/TesterCriterion.mqh` |
| **T-09** MH Attention | MHA (8 голов, window_out=8, model_dim 32) в общем рантайме; smoke-сравнение fc/lstm/mha на синтетике END-TO-END (MSE 1.078/1.067/1.102 — ожидаемо ≈1 на random walk) | `scripts/run_book_experiments.py` |
| **T-10** Порог + day-of-week | `passes_trade_level` (0.6), `day_of_week_stats`, `blocked_days_from_stats` (блок только при WR<45% **И** PnL<0 **И** ≥30 трейдов; fail-open); MQL5-зеркало `DayFilter.mqh` | `model/day_of_week_filter.py`, `mql5/NeuroTrader/DayFilter.mqh` |
| **T-11** Gradient check в CI | Автотесты всех стеков (FC/LSTM/MHA/GPT/композит) + тест, что чекер ЛОВИТ сломанный backward (саботаж ×2 — тот самый класс бага /scale) | `model/book_nn/tests/test_gradient_check_ci.py` |
| **T-15** Новостной фильтр | Live: `NewsGuard.mqh` (календарь, ±30 мин, high-importance USD, merge окон, в тестере fail-open с пояснением). Бэктест: `NewsStore` (WAL, UNIQUE(ts,title,country), `events_between`, `import_csv`, `is_news_blackout`) | `mql5/NeuroTrader/NewsGuard.mqh`, `data/news_sqlite.py` |
| **T-16** Python-мост | `SignalBridgeWriter`: таблица `ml_signals` + `bridge_meta(schema_version=1)`, WAL, идемпотентная запись (retry не сбрасывает статус EA), TTL 3ч, `expire_stale`; MQL5-читатель с fail-closed по версии схемы; автоторговля Python не используется (10027 — фича). **End-to-end продюсер** `scripts/publish_book_signals.py`: свечи → фичи → нормализация (train-параметры) → ансамбль FC+LSTM+MHA → `ensemble_vote` → `SignalIntent` с ATR-SL/TP и `features_hash`; идемпотентный `intent_id`, fail-closed (flat → ничего не пишется) | `execution/signal_bridge.py`, `mql5/NeuroTrader/SignalBridge.mqh`, `scripts/publish_book_signals.py` |
| **T-17** Train/Val-расхождение | Мониторинг в цикле обучения: patience/min_train_progress/val_worsen_ratio, алерт с эпохой и обеими кривыми | `model/book_nn/train.py` |
| **T-19** Расширение фичесета | `atr_ratio`, `session_vol_ratio`, `volume_ratio` — масштабно-свободные, по схеме нормализации T-02 (`--extended-features`); реальные данные вскрыли и починили 2 бага (взрыв CV до 4.2e6 на «мёртвых» окнах, head-NaN ATR) — регресс-тесты, п. 3.4 | `model/sample_generator.py` |
| **T-20** Конфигурация моделей | `BookNetwork` из CLayerDescription-диктов, сериализация `.npz`+`.json` (архитектура как данные), `book_fc/lstm/mha_description` | `model/book_nn/network.py` |
| **T-23** Walk-forward + дрейф | `psi` (0.10/0.25), `ks_statistic` (scipy+exact fallback), `feature_drift_report`, `normalization_shift` (scale ratio >1.5 или mean shift >1σ → alarm), `walk_forward_drift_gate` (блокирует deploy); сам walk-forward-конвейер уже был в репо (`backtest/walk_forward.py`, `scripts/retrain_real_trades.py`) — интеграция дрейфа добавлена; прогнано на РЕАЛЬНЫХ данных: PSI 0.318 → alarm, `deploy_blocked=true` (п. 3.5) | `model/drift.py` |
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

## 3. Эксперименты на реальных данных (разблокировано)

### 3.1 Источник данных

Прямые провайдеры (Yahoo/stooq/Binance) из песочницы недоступны (TLS
блокируется; Yahoo `XAUUSD=X` интрадей не отдаёт, v7/download требует
crumb). Разблокировка — через публичное зеркало Kaggle-датасета на GitHub:
`github.com/BaseMax/XAUUSD-LSTM`, файл `XAU_15m_data.csv` — MT4-экспорт
XAUUSD **M15, 2004-06-11 … 2025-09-30, 480 717 баров**.

Импортёр `scripts/import_external_candles.py` (тестами покрыт):
семиколоночный CSV → `data/market_data_external.sqlite` (таблица `candles`,
та же схема, что читают лоадеры; `symbol='XAUUSD'`, `timeframe='M15'`),
OHLC-инварианты проверяются — 0 баров отклонено; провенанс в
`source_meta` (источник, даты, счётчики); идемпотентный re-import;
`data/market_data_mt5.sqlite` не трогается (терминальные и внешние данные
раздельно).

**Оговорка:** таймфрейм — M15, а не M5 из ТЗ: публичного M5-датасета
XAUUSD нужной глубины не найдено (поиск по GitHub выдаёт только EA, без датасетов).
Все скрипты таймфрейм-параметризованы (`--timeframe`), пайплайн от этого
не меняется; при подключении терминала M5-ветка запускается той же
командой.

### 3.2 Прогон (последние 80 000 баров ≈ 2.3 года, до 2025-09-30)

`python -m scripts.run_book_experiments --db data/market_data_external.sqlite
--asset XAUUSD --timeframe M15 --max-bars 80000 --epochs 25` (добавлен
`--max-bars` — хвостовой срез в `run_book_experiments` и
`create_initial_data_xauusd`). Сплит 60/20/20 по времени: 47 984 /
15 995 / 15 994 сэмпла; window 16, горизонт [6, 12] баров, 7 базовых фич.

| Модель | test MSE | dir.acc h=6 | dir.acc h=12 |
|---|---|---|---|
| Наивный прогноз (ноль) | **2.006** | 0.500 | 0.500 |
| FC (6 902 парам.) | 2.081 | 0.505 | 0.503 |
| LSTM (6 242 парам.) | 2.415 | 0.495 | 0.502 |
| MHA (12 990 парам.) | 2.832 | 0.493 | 0.491 |

Книги должны читаться в этих числах так:

1. **Ни одна архитектура не обыгрывает наивный прогноз** (MSE и
   direction hit-rate ≈ 50%). Это подтверждение вывода синтетического
   smoke-теста уже на реальном рынке: на XAUUSD окно прошлых возвратов
   не даёт предсказательной силы для мультигоризонтного возврата.
   Пайплайн при этом работает корректно и — важно — **не выдумывает
   edge**: модели честно не бьют базовую линию.
2. **Мониторинг train/val-расхождения (T-17) сработал на реальных
   данных**: предупреждение «train 0.80 ↓ при val 1.20 stuck» после 23
   «stale»-эпох — механизм книги (p. 256) ловит переобучение в бою.
3. **Сдвиг режима**: дисперсия тест-выборки [1.87, 2.14] против 1.0 на
   train (2025-й — резкий рост волатильности золота) — ровно тот случай,
   для которого нужны дрейф-гейты T-23 (`psi`/`normalization_shift`).
4. Roundtrip сериализации верифицирован: MSE перезагруженных из `.npz`
   моделей побитно совпадает с summary (`2.0811/2.4146/2.8321`).

Артефакты: `output/book_experiments_real/` (3 модели + кривые + summary),
`data/book_initial/` (samples.npz, `book_normalization.json`,
`book_day_filter.json` — теперь из реальных данных; `dataset_meta.json`:
`source=sqlite:…external.sqlite, synthetic=false`).

### 3.3 T-16 end-to-end на реальных данных

`python -m scripts.publish_book_signals --models-dir
output/book_experiments_real --db data/market_data_external.sqlite`:

```
ensemble flat (p_up=0.559, agreement=0.00) - nothing sent
votes: fc=+1, lstm=+1, mha=-1
```

Итог ровно тот, что проектировался: порог TradeLevel 0.6 не взят, члены
ансамбля не согласны → **flat, в мост ничего не пишется** («no edge, no
trade»). При взятом пороге продюсер пишет `SignalIntent` с
ATR-SL/TP, `features_hash` и идемпотентным `intent_id` (повторный запуск
на том же баре не дублирует строку) — поведение зафиксировано тестами
(`scripts/tests/test_publish_book_signals.py`, 7 тестов).

### 3.4 Extended-фичи (T-19) и два багфикса от реальных данных

Тот же срез 80 000 баров, `--extended-features` (10 фич вместо 7):

| Модель | test MSE base → ext | dir.acc h=12 base → ext |
|---|---|---|
| Наивный (ноль) | 2.006 | 0.500 |
| FC | 2.081 → 2.182 | 0.503 → 0.502 |
| LSTM | 2.415 → 2.604 | 0.502 → 0.493 |
| MHA | 2.832 → 2.400 | 0.491 → **0.517** |

Extended-набор edge не даёт (все MSE ≥ наивного). Отдельно: dir.acc
MHA на h=12 вышел на 0.517 — это ≈2σ над монеткой на 16k сэмплов, но без
подтверждения MSE и на одном прогоне это не сигнал, а повод для
отдельной проверки.

**Первый запуск на реальных данных вернул test MSE = NaN по всем трём
моделям** — диагностика вскрыла два бага T-19-фичесета, которые
синтетический smoke не ловил (это главный практический результат
extended-прогона):

1. `session_vol_ratio` = std/|mean| взрывался до **4.2·10⁶** на
   «мёртвых» окнах (скользящее среднее возвратов → 0) — z-score и обучение
   отравлены. Фикс: знаменатель флоорится 5%·std того же окна + clip на
   20 (scale-free, ограничен сверху).
2. `_atr(min_periods=period)` оставлял 13 NaN в голове фрейма → NaN-лоссы
   первых windowed-сэмплов. Фикс: `min_periods=1` (частичное EMA, строго
   каузально).

Оба фикса закрыты регресс-тестами
(`scripts/tests/test_book_realdata_scripts.py`: нет NaN в голове,
ограниченность на плоских окнах). Честная оговорка: после фикса
`session_vol_ratio` в основном сатурируется у 20 (p99 = 19.5) —
информативность низкая, кандидат на переопределение (например,
std₃₂/std₂₅₆) в следующей итерации; семантику менять сейчас не стал
(ограничение «без радикальных изменений»).

### 3.5 Дрейф-гейт (T-23) на реальных данных

`scripts/run_book_drift_report.py` — train (60%) против test (20%) того же
среза, теми же фичами, что в обучении:

| Метрика | Значение | Порог | Статус |
|---|---|---|---|
| worst PSI | **0.318** | alarm > 0.25 | **alarm** |
| worst KS | 0.179 | — | — |
| normalization scale ratio (MACD) | **2.40×** | > 1.5 | alarm |
| worst mean shift | 0.17σ | > 1σ | ok |

Вердикт гейта: `status = alarm`, **`deploy_blocked = true`**. Т.е.
автоматика T-23 ровно на тех окнах, где модели не смогли обыграть наивный
прогноз, сама запрещает деплой — цепочка «сдвиг режима → блокировка»
работает на реальном рынке (волатильность тест-окна в 2.4 раза выше
train-овой по MACD-колонкам — тот самый 2025-й из п. 3.2(3)).

### 3.6 Бэктест ансамбля с приёмочным вердиктом (T-25 → T-16 → T-03/T-08)

`scripts/run_book_ensemble_backtest.py` — сигналы генерируются ЦЕПОЧКОЙ
ПРОДЮСЕРА (TradeLevel 0.6, min_agreement 0.6, ATR-геометрия 1.5×/2:1),
вход по open следующего бара (без заглядывания вперёд), полный спред
$0.30 за раунд-трип, при неоднозначном баре первым заполняется SL
(пессимистично), одна позиция одновременно (зеркало EA). Торговался
только test-срез (2024-09-18 … 2025-09-30):

| Метрика | Значение |
|---|---|
| Сигналов → сделок (178 отклонено: позиция занята) | 243 → **65** |
| Profit factor | 1.239 |
| Win rate | **41.5%** |
| Net PnL (баланс 100) | +76.3 |
| Max DD | 55.1% |
| Критерий T-08 (PF·√N − w·DD) | −45.1 |
| Buy&hold за то же окно | **+49.7%** |
| **Вердикт T-03** | **FAIL** (WR 0.415 ≤ 0.55) |

Направления: 39 short / 26 long; выходы: 32 time / 24 SL / 9 TP; средняя
длительность 8.8 бара.

Интерпретация: PF формально выше порога 1.2, но win rate провален, DD
54%, а +76 пунктов на окне, где buy&hold дал +50% (золото 2600→3850) —
это выглядит бета-захватом тренда-2025 (спот ~2570→3844), а не альфой; дрейф-гейт из 3.5
корроборирует. Вердикт всей цепочки: **модель не деплоится** —
приёмочный контур T-03/T-08 делает свою работу на реальных данных,
а не только в юнит-тестах.

## 4. Заблокировано / отложено

| Задача | Причина |
|---|---|
| **T-24** GPT-блоки | Отложено **по условию самого ТЗ**: «после стабилизации walk-forward и при наличии GPU». Слой `GPTStyleBlock` написан и проверен градиент-чеком (4.73e-8) — блокировки на уровне библиотеки нет, не запускается только применение. |
| **Компиляция MQL5** | В Linux-песочнице нет MetaEditor. Синтаксис выверен по документации API (в процессе исправлены несуществующие `DatabaseReadExecute`/`DatabaseChanges`/`StringTrim` — заменены на документированные эквиваленты), баланс скобок проверен. Требуется одна контрольная сборка в терминале. |
| **Замер ускорения OpenCL** | Нужен терминал с GPU-драйвером; скрипт `BenchmarkOpenCL.mq5` готов и пишет CSV. |

## 5. Верификация

**Новые тесты: 102 passed** (`pytest` по книгам-интеграции):

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
scripts/tests/test_publish_book_signals.py        7   (T-16 e2e + импортёр внешних данных)
scripts/tests/test_book_realdata_scripts.py       11  (T-23 drift-отчёт, T-25+T-03 бэктест, багфиксы T-19)
```

Градиентные проверки (`model/book_nn`): FC 2.5e-8, LSTM 6.4e-8, GPT
4.73e-8, MHA ≤3.66e-5 (6 seed'ов, tol 1e-4), композит MHA+LSTM+FC 1.06e-5.

**Регресс:** полный прогон затронутых пакетов — 816 passed / 10 failed / 1 skipped;
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

## 6. Как это запускать

```bash
# импорт внешней истории (MT4-CSV, напр. Kaggle-зеркало XAUUSD M15 2004-2025)
python -m scripts.import_external_candles --csv XAU_15m_data.csv \
    --db data/market_data_external.sqlite --symbol XAUUSD --timeframe M15 \
    --source-url "https://github.com/BaseMax/XAUUSD-LSTM"

# датасет + артефакты для EA (нормализация, day filter, samples.npz)
python -m scripts.create_initial_data_xauusd --out-dir data/book_initial

# эксперименты FC/LSTM/MHA (реальные данные: external.sqlite, M15)
python -m scripts.run_book_experiments --db data/market_data_external.sqlite \
    --asset XAUUSD --timeframe M15 --max-bars 80000 --epochs 25 \
    --out-dir output/book_experiments_real

# продюсер сигналов в мост (T-16 end-to-end: ансамбль -> ml_signals)
python -m scripts.publish_book_signals --models-dir output/book_experiments_real \
    --db data/market_data_external.sqlite --asset XAUUSD --timeframe M15

# дрейф-гейт T-23 на тех же окнах (PSI/KS + сдвиг нормализации)
python -m scripts.run_book_drift_report --db data/market_data_external.sqlite \
    --asset XAUUSD --timeframe M15 --max-bars 80000 \
    --out output/book_drift_report/report.json

# бэктест ансамбля с приёмочным вердиктом T-03 и критерием T-08
python -m scripts.run_book_ensemble_backtest \
    --models-dir output/book_experiments_real \
    --db data/market_data_external.sqlite --asset XAUUSD --timeframe M15 \
    --max-bars 80000 --out output/book_ensemble_backtest/report.json

# то же с extended-фичами (T-19)
python -m scripts.run_book_experiments --db data/market_data_external.sqlite \
    --asset XAUUSD --timeframe M15 --max-bars 80000 --epochs 25 \
    --extended-features --out-dir output/book_experiments_real_ext

# экспорт обученной FC для EDGE-режима EA (OpenCL/CPU)
python -m scripts.export_fc_weights --model output/book_experiments_real/book_fc \
    --out book_fc_weights.bin

# тесты
python -m pytest model/tests model/book_nn backtest/tests data/tests \
    execution/tests scripts/tests mql5/tests config/tests
```

EA: `mql5/NeuroTrader/NeuroTraderEA.mq5`, режимы BRIDGE (Python-сигналы
через `ml_signal_bridge.sqlite`) и EDGE (локальный инференс). Артефакты
для `MQL5\Files`: `book_normalization.json`, `book_day_filter.json`,
`book_fc_weights.bin` — см. `mql5/NeuroTrader/README.md`.

## 7. Конфигурация

Секция `books:` в `config/config.yaml` (samples/trade_level/day_filter/
news_guard/validation/tester_criterion/bridge/drift) + accessor
`config.loader.books_config()` с дефолтами и отказом на неизвестные ключи.
Существующие секции не тронуты.
