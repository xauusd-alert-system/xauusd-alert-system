# План работ и статус реализации (TODO & Roadmap)
## Проект: `xauusd-alert-system`

**Текущий статус (аудит 2026-08-16):** кодовые компоненты в основном реализованы, но это **не означает подтверждённую доходность или live-ready**. Causal grid-parity fix инвалидировал старые XAU/BTC admission gates; automatic retraining и model-driven execution заморожены до повторной pre-lock revalidation. Последняя локальная проверка: **545 tests passed**.

Статусы ниже различают: `IMPLEMENTED` (код и тесты), `RESEARCH-ONLY`, `OPT-IN`, `LIVE-VERIFIED`. Если `LIVE-VERIFIED` явно не указан, модуль нельзя считать проверенным на реальном счёте/рынке.

---

## 1. Матрица готовности подсистем

| Подсистема / Модуль | Описание функционала | Тесты | Статус |
|---|---|---|---|
| **Telegram Clean Signal Formatter** (`alerts/formatter.py`) | Форматирование сигналов ШОРТ/ЛОНГ с равным шагом TP1/2/3 и Stop Loss | `test_formatter` | `ГОТОВО [x]` |
| **Data Ingestion & Storage** (`data/`) | MT5 провайдер, SQLite хранилище, сессионный таггер, news filter, логгеры сделок/сигналов | `test_ingestion`, `test_storage`, `test_signal_log`, `test_trade_logger` | `ГОТОВО [x]` |
| **Macro AI News Sentiment** (`data/sentiment_analyzer.py`) | Анализатор макроэкономических новостей, заявлений ФРС/ЕЦБ, инфляции и геополитики | `test_sentiment_analyzer` | `ГОТОВО [x]` |
| **Causal Feature Engineering** (`features/`) | Индикаторы (EMA, RSI, MACD, ATR, BB, ADX, Donchian), анатомия свечей, рыночная структура, MTF confluence | `test_no_lookahead` (строгая проверка отсутствия заглядывания в будущее) | `ГОТОВО [x]` |
| **Order Flow & Microstructure** (`features/order_flow.py`) | Cumulative Volume Delta (CVD), Order Flow Imbalance, rolling VWAP и стандартные полосы отклонения | `test_order_flow` | `ГОТОВО [x]` |
| **Market Regime Classifier** (`regime/`) | Классификация режимов (trend_up/down, range, compression, reversal_watch, no_trade) | `test_classifier` | `ГОТОВО [x]` |
| **Unsupervised Regime GMM** (`regime/hmm_classifier.py`) | GaussianMixture research prototype; это НЕ HMM, нет transition/Viterbi и production runtime import | `test_hmm_classifier` | `RESEARCH-ONLY; требуется rename/remove decision` |
| **Offline Labeling** (`labeling/`) | Versioned barrier/traded events; traded geometry требует asset_key и costs | `test_labels`, `test_traded_label_space` | `IMPLEMENTED; economic A/B pending` |
| **Model Training & Calibration** (`model/`) | Purged split, uniqueness weights в base+calibration, обязательный OOS report/metadata bundle | `test_trainer`, `test_production_contract` | `IMPLEMENTED; new baseline pending` |
| **Neural & Hybrid Ensemble** (`model/neural_trainer.py`) | MLP prototype; не включён в production baseline | `test_neural_trainer` | `RESEARCH-ONLY; не приоритет текущего цикла` |
| **Ensemble & Meta-Filter** (`model/ensemble.py`) | Комбинирование ML + Rules, EV gate, dynamic min confidence, hard divergence veto, probability normalization, news guard | `test_ensemble`, `test_ensemble_backtest` | `ГОТОВО [x]` |
| **Multi-Broker Execution Layer** (`execution/broker_adapter.py`) | MT5 + virtual adapters; FIX/cTrader поверхности являются mock/prototype | `test_broker_adapter` | `IMPLEMENTED; MT5 only operational path` |
| **Portfolio & Risk Allocation** (`execution/portfolio_allocator.py`) | Kelly/inverse-vol/HRP utilities | `test_portfolio_allocator` | `OPT-IN/RESEARCH; HRP не приоритет без active multi-asset portfolio` |
| **Execution & Risk Management** (`execution/`) | MT5 авто-трейдер, трехуровневый TP (50%/30%/20%), Breakeven, trailing stop, dynamic correlation filter, daily loss circuit breaker | `test_engine`, `test_virtual_mt5_shim` | `ГОТОВО [x]` |
| **Monte Carlo Stress Testing** (`backtest/monte_carlo.py`) | Стресс-тестирование, VaR 95%/99%, CVaR (Expected Shortfall), Risk of Ruin, симуляция 1000 эквити-кривых | `test_monte_carlo` | `ГОТОВО [x]` |
| **Alerts & Visual Charts** (`alerts/`) | Telegram рассылка, интерактивный бот (`/start`, `/status`, `/metrics`, `/pause`, `/resume`, `/closeall`), SVG/ASCII визуализатор уровней | `test_formatter`, `test_chart_renderer` | `ГОТОВО [x]` |
| **Interactive Web Dashboard & API** (`realtime/app.py`, `dashboard.py`) | API/UI с обязательными source/mode/as-of disclosures; отсутствие live data не подменяется demo-числами | `test_app` | `IMPLEMENTED; deployment not asserted` |
| **LOB Simulation & MT5 Shim** (`simulation/`) | Synthetic matching/shim test environment, не источник подтверждённого alpha | `test_virtual_mt5_shim` | `IMPLEMENTED FOR TESTING` |
| **Deploy Guard & Overnight Pipeline** (`scripts/`) | Backup/retrain/OOS guard/rollback orchestration | `test_deploy_guard`, `test_retrain_real_trades`, `test_scheduler` | `IMPLEMENTED; live verification environment-dependent` |

---

## 2. Реализованная спецификация шага тейк-профитов и стоп-лосса

На основе анализа примеров сделок реализована следующая сетка:

- **Базовый шаг тейк-профита ($\Delta_{TP}$):**

  - $\Delta_{TP} = 1.0 \times \text{Step}$ (динамически $1.0 \times ATR$, для Gold на $4250 \approx 4.25$ пт).

  - $\text{TP1} = \text{Entry} \pm 1.0 \times \text{Step}$

  - $\text{TP2} = \text{Entry} \pm 2.0 \times \text{Step}$ (ровно $2\times$ от TP1)

  - $\text{TP3} = \text{Entry} \pm 3.0 \times \text{Step}$ (ровно $3\times$ от TP1)

- **Стоп-лосс ($\Delta_{SL}$):**

  - $\text{Stop Loss} = \text{Entry} \mp 3.0 \times \text{Step}$ (ровно $3\times$ шага от точки входа, что дает соотношение риска к TP3 $1:1$, а при фиксации TP1 и переводе в безубыток — безрисковую позицию).

- **Конфигурация шага (гибрид):** по умолчанию шаг динамический ($1.0 \times ATR$); при необходимости перекрывается per-asset параметром `step_points` (в пунктах цены) и ограничивается клампами `step_min_points` / `step_max_points` (секция `signal_grid` в `config/config.yaml`, per-asset оверрайды — в `assets.<key>.signal_grid`). Тренировочный target versioned: global `barrier` сохраняет legacy 1.2/1.0, а explicit per-asset `traded` использует реальную execution geometry/cost contract; выбранный event и config hash записываются в model bundle. Метаданные (Conf / Regime / Session) в сообщение Telegram добавляются опционально флагом `alerts.include_signal_meta` (по умолчанию `false` — чистый формат по ТЗ).

- **EV gate считает payoff по TP3/стоп** (`signal_grid.tp3_mult / stop_mult`): при равной сетке это ровно $1:1$ (риск:TP3), поэтому фильтр работает как фильтр качества вероятности (p должна превышать $0.5 + threshold/2$). Расчёт по TP1 (как было) при новой сетке давал бы $1/3$ и отклонял бы все сделки. Бэктест-движки (`backtest/engine.py` и `model/ensemble_backtest.py`) используют ту же `signal_grid`-сетку (TP1/2/3 = 1/2/3 шага, стоп = 3 шага, TP1 50% + безубыток, TP2 30%, TP3 20%), поэтому цифры бэктеста соответствуют реальному исполнению в MT5.

- **Формат отправки в Telegram:**

```text

ШОРТ

GOLD | ЗОЛОТО | XAUUSD

Зона входа: 4255.66

Цели:

→ TP1: 4251.4

→ TP2: 4247.14

→ TP3: 4242.89

Стоп: 4268.42

```

```text

ЛОНГ

GOLD | ЗОЛОТО | XAUUSD

Зона входа: 4263.28

Цели:

→ TP1: 4267.54

→ TP2: 4271.8

→ TP3: 4276.06

Стоп: 4250.49

```

---

## 3. Реализованные фазы и улучшения

- [x] **Фаза 0: Устранение утечек данных и честная валидация** (Purged Time-Split, изоляция моделей бэктеста, очистка BOM).
- [x] **Фаза 1: Мульти-активная калибровка и контрактные спецификации** (индивидуальный `slippage_usd` и `point_value_lot` для XAU, XAG, BTC, EUR, GBP).
- [x] **Фаза 2: Фильтр математического ожидания (EV Gate) и 3-классовые модели**.
- [x] **Фаза 3: Обучение с учетом рыночных режимов и обратная связь по реальным сделкам**.
- [x] **Фаза 4: Усиление ансамбля и защита от расхождений** (`hard_divergence_veto`, `dynamic_min_confidence`, сессионный фильтр).
- [x] **Фаза 5: Нормализация вероятностей и аудит сигналов** (`normalize_probs`, observability warnings).
- [x] **Фаза 6: Автоматический Deploy Guard и ночной пайплайн** (`deploy_guard.py`, systemd timer).
- [x] **Фаза 7: Нейросетевой классификатор и гибридный ансамбль** (`model/neural_trainer.py`).
- [x] **Фаза 8: Анализ потока ордеров (Order Flow, CVD, Imbalance, rolling VWAP)** (`features/order_flow.py`).
- [x] **Фаза 9: Мульти-брокерский слой исполнения (MT5, FIX 4.4, Simulation)** (`execution/broker_adapter.py`).
- [x] **Фаза 10: Интерактивный Web Dashboard, живые графики и WebSocket API** (`realtime/app.py`, `dashboard.py`).
- [x] **Фаза 11: Динамическое портфельное управление и риск-паритет** (Критерий Келли, Inverse Volatility, HRP).
- [x] **Фаза 12: Моделирование методом Монте-Карло и расчет VaR/CVaR** (`backtest/monte_carlo.py`).
- [x] **Фаза 13: Кластеризация скрытых режимов рынка (GMM / Unsupervised)** (`regime/hmm_classifier.py`).
- [x] **Фаза 14: ИИ-анализ тональности макроэкономических новостей** (`data/sentiment_analyzer.py`).
- [x] **Фаза 15: Визуализация торговых сетапов и уровней (SVG / ASCII)** (`alerts/chart_renderer.py`).
- [x] **Фаза 16: Кастомный чистый формат сигналов для Telegram** (`alerts/formatter.py`): сетка TP1/2/3 с равным шагом и стоп-лоссом $3.0 \times \text{Step}$, отправка в формате ШОРТ/ЛОНГ.
- [x] **FX v3 — ранний безубыток + стоп 2.0 + H1 для EUR/GBP (механика ВЫХОДА):** var-2 фильтры входов (0.92 / EV 0.10 / hard veto) перевеса не дали (EUR exp −0.26, 0/14; GBP exp −0.24, 2/14) — при WR 62–66% сетка 1:3 математически убыточна ДО издержек (хвост −3×шага ≈ 6× среднего выигрыша). Пакет атакует хвост потерь только для `assets.EURUSD`/`assets.GBPUSD`: `timeframe: H1`, `signal_grid.stop_mult: 2.0`, новый `signal_grid.breakeven_trigger_atr: 0.5` (SL → entry после 50% дистанции до TP1, ДО TP1), `labeling.horizon_candles_n: 48`, `ensemble.min_confidence_to_alert: 0.85` без `ev_threshold`/`hard_divergence_veto` (убраны; наследуют глобальные 0/false). Реализовано в трёх движках: `config/loader.py::get_signal_grid` (ключ `breakeven_trigger_atr`, дефолт 1.0), `model/ensemble_backtest.py` (exit_reason `"breakeven"`; дефолт 1.0 = бит-в-бит legacy → XAU/BTC/XAG не меняются), `backtest/engine.py` (ранний BE, ярлыки выхода не менялись), `execution/mt5_trader.py` (`be_trigger_by_symbol`, SL в ноль только при trigger < 1.0). XAU/XAG/BTC не трогались. Перезамер: `python -m scripts.run_backtest --asset EURUSD/GBPUSD` (см. `docs/FX_V3.md`); решение по `exp>0, PF>1, плюсовых фолдов ≥5/14`.
- [x] **Фаза 17: Order-flow фичи в моделях + per-asset таймфреймы** (`features/order_flow.py` → обучение и инференс): CVD, CVD-slope, Order Flow Imbalance 14/50, дистанция до VWAP по ATR добавлены в `FEATURE_COLUMNS` (41 → 46 фич) и в пайплайн (`train_mt5`, `run_backtest`, `realtime/pipeline`). `assets.<key>.timeframe` позволяет торговать FX/XAG на M15 (издержки ~100% шага на M5 падают до ~30–40%), XAU/BTC остаются на M5; подхвачено в `train_all_assets`, `run_backtest`, `overnight` (бэкфилл по всем таймфреймам), `retrain_with_real_trades`, `deploy_guard`, `seed_db`.
- [x] **FX var2 (tighten) — ужесточение фильтров EUR/GBP (вариант 2, SUPERSEDED by FX v3):** только per-asset `assets.EURUSD.ensemble` и `assets.GBPUSD.ensemble` → `min_confidence_to_alert: 0.85→0.92`, `ev_threshold: 0→0.10`, `hard_divergence_veto: false→true`; глобальные `ensemble` без изменений; merge через `merge_asset_cfg` / `RealtimePipeline.effective_cfg`; `compute_ensemble_signal` читает `ens_cfg` после merge (покрыто 5 тестами). XAU/XAG/BTC не трогались. Ожидаем сдвиг EUR (exp −0.26, 0/14) / GBP (exp −0.24, 2/14) к `exp>0, PF>1, ≥5/14`; перезамер: `python -m scripts.run_backtest --asset EURUSD` / `GBPUSD` (решение — отдельным коммитом, см. `docs/benchmarks.md`).

---

## 4. Чек-лист проверки и эксплуатации

- [x] **Тестовый набор:** `pytest -q` — **545 passed, 11 warnings** (2026-08-16; warnings: малые synthetic CSCV fixtures и Starlette deprecation). Исторические counts в change log не являются текущим статусом.
- [x] **Веб-дашборд:** код/API реализованы; фактический deployment status определяется `/health`, а каждое значение обязано показывать `source/mode/as_of` — постоянный ONLINE здесь не утверждается.
- [x] **API эндпоинты:** `/health`, `/signal`, `/api/matrix`, `/api/correlation`, `/api/paper-status`, `/api/status`, `/api/sentiment`, `/api/monte-carlo`, `/api/chart/XAUUSD` — **Все работают**.
- [x] **Симуляция LOB:** `python -m scripts.run_simulation` — **Проверено**.
- [x] **Ночной таймер:** `deploy/overnight/overnight.timer` — **Сконфигурирован**.
