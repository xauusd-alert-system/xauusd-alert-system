# План работ и статус реализации (TODO & Roadmap)
## Проект: `xauusd-alert-system`

**Текущий статус (аудит 2026-08-16; обновлено 2026-08-18):** кодовые компоненты в основном реализованы, но это **не означает подтверждённую доходность или live-ready**. Causal grid-parity fix инвалидировал старые XAU/BTC admission gates; automatic retraining и model-driven execution заморожены до повторной pre-lock revalidation (исключение — 48h demo trial, `scripts/trial_window.py`, до 2026-08-21T23:59:59+00:00). Последняя локальная проверка: **888 tests passed**.

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

## 3b. MQL5 observer wave (2026-08-16, план в `docs/MQL5_OBSERVER_PLAN.md`)

- [x] **Контракты v1:** `SignalIntent` / `ExecutionEvent` / `EventEnvelope` + детерминированные `event_id` (`contracts/execution_contracts.py`, 12 тестов).
- [x] **Wave 1 — MQL5 Observer EA (read-only):** `mql5/SignalDeskObserver/` (ObserverEA, EventSerializer, DiskOutbox, SymbolResolver, HistoryReconciler, JsonWriter; без `OrderSend`/`CTrade`; demo/contest-only; wire-contract golden tests). Компиляция и acceptance — на Windows-хосте (`mql5/SignalDeskObserver/README.md`).
- [x] **Wave 0 — provenance manifest:** `data/provenance.py` + `scripts/build_provenance_manifest.py`; opt-in gate в `train_mt5`/`run_backtest` (`validation.require_provenance_manifest`).
- [x] **Wave 2 — ledger bridge:** `data/ledger_bridge.py` (outbox + HMAC + retry), `scripts/run_ledger_bridge.py`, серверные endpoints `/api/ledger/ingest`, `/api/ledger/events`, `/api/ledger/execution-quality`, `/api/ledger/lifecycle/{intent_id}` (`realtime/app.py`, `data/ledger_events.py`), Python sender пишет `intent_created`/`request_result` и intent до `order_send` (`execution/mt5_trader.py`, `data/intent_ledger.py`, `data/execution_ledger.py` intent/precision columns).
- [x] **`signal_journal.asset_key`:** nullable-колонка + in-place миграция (`logs/journal.py`).
- [ ] **Wave 3+ (требуют Windows-хоста):** acceptance checklist, empirical cost dataset на demo, frozen paper baseline, revalidation. UI-views Execution Quality / Lifecycle Trace — API готов, HTML — будущая работа.

## 3c. Web-UI honesty wave (2026-08-16, спецификация «Полная реализация личного веб-интерфейса», §12/§6.3; аудит в `docs/WEB_UI_HONESTY_AUDIT.md`)

- [x] **DataEnvelope/freshness контракт:** `realtime/data_envelope.py` — `fresh/stale/offline/waiting/error` (5s/60s), ключи `source/mode/as_of_utc_ms/freshness_status/ingest_lag_ms/coverage/last_successful_at_utc_ms`; применён ко всем dashboard endpoints и ledger endpoints.
- [x] **`/api/matrix` без neutral fallback:** per-asset `status=error` + `reason` при исключении, `status=unavailable` вне live-режима; `bias/confidence=None` — никогда не `neutral 0.50`.
- [x] **Mutation controls отключены:** `/api/control/*` → `501` для всех действий (даже с токеном), `403` без токена; до command bus браузерных controls не существует (Telegram-бот остаётся единственным управлением). Из dashboard удалён мёртвый `sendControl`, добавлен баннер `INTERNAL DIAGNOSTIC VIEW`.
- [x] **`/ws` — ledger event stream:** owner-only (`?token=`), push нормализованных `ledger_events` каждые 2s с freshness/heartbeat; без токена — `UNAUTHORIZED` + close 1008; строгий курсор без дубликатов. Требует `websockets`/`uvicorn[standard]`.
- [x] **No-fallback тесты:** `realtime/tests/test_data_envelope.py`, `test_dashboard_honesty.py`; WS-тесты в `test_ledger_ingest.py`; control-тест в `test_app.py` обновлён (501).
- [ ] **Signal Desk UI (другой проект `trading-system-playbook`):** страницы Overview/Signals/Lifecycle/Positions/Execution Quality/Risk/Research/System Health — вне этого репозитория.
- [ ] **Wave 3+:** MT5 read-only views на Windows-хосте, empirical cost dataset, acceptance checklist.

## 3d. TradeGroupSpec v1 (2026-08-16, ТЗ «TradeGroupSpec v1»; статус в `docs/TRADE_GROUP_SPEC.md`)

- [x] **Domain-контракт:** `execution/trade_group.py` — `TradeGroupSpec` v1 (immutable), `TradeLeg`, `GroupState` (state machine + BE_RETRY), `BreakEvenPolicy`, `GroupRisk`, `allocate_leg_volumes` (floor-правило), `check_group_risk` (один раз на группу), ids `signalId/intentId/groupId/legId`.
- [x] **Geometry engine:** `execution/trade_geometry.py` — pure: профили, step (ATR+clamps), TP1/2/3/SL, tick alignment, broker min stop distance, cost-aware admissibility, gross R, reason codes; `build_trade_group_from_signal()` — ML→spec мост.
- [x] **Profiles:** `config.trade_profiles` — `xau_m15_intraday_v1` (validated), `btc_m5_scalp_v1` (validated:false, paper-only кандидат; live BTC signal_grid не тронут).
- [x] **BE (ТЗ §17/§18):** raw = actual fill; protected = fill + spread + slippage + commission; BE_CONFIRMED только после modify + broker query; bounded retry.
- [x] **Telegram parity:** `alerts/formatter.py` — authoritative final geometry для `trade-group.v1` (recomputation запрещена), legacy fallback только для старых сигналов, lifecycle update формат.
- [x] **Ledger/logger:** 15 новых lifecycle event types + `group_id`/`leg_id` (nullable, in-place); `trade_logger` group-колонки.
- [x] **Persistence + paper executor:** `data/trade_group_store.py` (submitted-guard против duplicate orders при restart), `execution/trade_group_executor.py` (PaperDriver netting/hedging, simulate_tick, restart recovery; demo по `TRADE_GROUP_ENABLE_DEMO=1`; live — `LiveExecutionForbidden`).
- [x] **Broker adapter:** `get_account_mode()`, `get_symbol_constraints()`.
- [ ] **P1.5 (отложено):** `mt5_trader.py` group execution (hedging/netting submit) — после acceptance; current live path неизменен.
- [ ] **P2:** BTC profile frozen-data validation (ТЗ §30), live promotion (только с отдельным подтверждением).

## 3e. TradeGroupSpec follow-up (2026-08-16): direction/BE/paper invariants

- [x] **SHORT SL validation fix:** явные direction-цепочки (LONG `SL<entry<TP1<TP2<TP3`, SHORT зеркально) вместо знаковой формулы.
- [x] **BE на все остаточные legs:** `confirm_break_even()` модифицирует+проверяет каждый `apply_to` ref (netting резолвит virtual legs).
- [x] **Demo env gate:** `TRADE_GROUP_ENABLE_DEMO` (fail-closed), live всегда заблокирован; spy-тест: paper path → 0 вызовов `order_send`.
- [x] **Parity helper:** Telegram читает уровни из `spec.as_geometry_payload()`.
- [x] **+58 тестов:** direction (LONG/SHORT), BE direction/alignment/costs, immutability против ATR/spread/candle, write-once fill, allocation dust (0.03–0.10), risk symmetry, live-gate, BTC 20–50 нетронут, SHORT Telegram parity, missing-geometry per-field, hedging lifecycle, no premature BE, BE retry→success, restart recovery matrix (6 состояний), ledger chronological/dedup.
- [x] **P1.5 — demo MT5 TradeGroup execution:** `execution/execution_intent.py` (intent + geometry-hash verification), `mt5_common.py` (fresh snapshot, account-mode detect, tick-align-only, ORDER_GEOMETRY_INVALID), `mt5_hedging_adapter.py` (3 physical legs), `mt5_netting_adapter.py` (aggregate + virtual legs, один BE modify), `mt5_trade_group.py` (demo executor + poll state machine), `reconciliation.py` (deal-history evidence, orphan detection, volume sync), store actions table (idempotency), ledger `leg_partially_filled/group_opened/orphan_broker_position/execution_error`. LIVE заблокирован; demo gate = `TRADE_GROUP_ENABLE_DEMO=1` + DEMO account. **+38 тестов** (34 demo-executor + 4 reconciliation; сценарии A–F §42, partial/reject/restart/orphan §43–§44, telegram §35–§36). Реальный Windows demo smoke test — на Windows-хосте.
- [x] **P1.5.1 — partial submission + volume reconciliation:** компенсационный flow `PARTIAL_SUBMISSION → COMPENSATION_REQUESTED → COMPENSATION_CONFIRMED → FAILED` (открытые legs закрываются market-close с broker confirmation; `COMPENSATE-L<n>`/`COMPENSATE-GROUP` actionIds через `mark_action`; `FAILED_WITH_OPEN_RISK` — non-terminal, reconciliation продолжается с bounded retry); volume ledger на группу и leg (`requested/filled/closed/remaining`); netting close volume от ACTUAL broker volume + cumulative allocation (`min(desired_increment, remaining)`, floor to volume_step, TP3 = весь остаток); hedging partial fill → `PARTIALLY_FILLED` с management по фактическому объёму; ledger events `partial_submission/compensation_requested/compensation_confirmed/compensation_failed/failed_with_open_risk`; Telegram partial/failed/emergency. **+29 тестов** (§22–§27). Реальный Windows demo smoke test — на Windows-хосте.
- [x] **P1.6 — ProvenanceSpec v1 / lineage / freshness:** `execution/provenance.py` (единый контракт source/sourceType/sourceId/mode/asOfUtcMs/observedAtUtcMs/freshness/dataHash/parentIds; frozen; legacy_unavailable для старых записей); `CostSnapshot.status` observed/estimated/unavailable + source (bare `CostSnapshot()` = unavailable, `COST_DATA_UNAVAILABLE` блокирует геометрию); `TradeGroupSpec.provenance` с lineage ids + раздельные `geometry_hash`/`provenance_hash` + `require_execution_provenance()`; `ExecutionIntent` provenance gate; ledger actor-vs-source колонки + TP/BE/stop события с `source=mt5/simulator`; `GET /api/provenance/{group_id}` (present/missing, owner-only); `scripts/verify_provenance.py`. **+30 тестов** (§43–§45). Реальные MT5 candle/broker IDs — на Windows-хосте; training manifest не придуман (явно unavailable).
- [ ] **P2:** BTC profile frozen-data validation (ТЗ §30), live promotion (только с отдельным подтверждением).

## 3f. Order book (DOM) overlay — Phase 0+1 (2026-08-18, план: «стакан в решения ML»)

- [x] **Phase 0 — сбор стакана:** `realtime/book_feed.py` — `BookFeed` (daemon-поток внутри трейдера), poll 3с, агрегация по M5-барам, персист в `data/book_bars/<MT5_SYMBOL>.csv` (persist=True только в трейдере), `bar_features` с on-demand финализацией закрытого бара, `overview` для статуса.
- [x] **Phase 1 — Book Gate (fail-open):** `realtime/pipeline.py::_apply_book_gate` — veto при |imb5_last| > veto_imbalance (0.35) против направления ансамбля, boost +0.05 (cap 0.95) при согласии; payload `book_gate`/`book_features`; активы без стакана — fail-open `unavailable` (на сигналы не влияют).
- [x] **Эмпирика FxPro demo (2026-08-18):** только BITCOIN отдаёт реальный стакан (10 уровней/сторону, `market_book_add`=True); XAUUSD/XAGUSD — add=False (стакана нет); EURUSD/GBPUSD — подписка есть, книга пустая. `book_gate` включён только для BTCUSD.
- [x] **Флаг подписки:** `market_book_add` может вернуть False, пока данные реально идут (терминал отдаёт DOM одному подключению); probe-чтение + флип `subscribed=True` при первом успешном снэпшоте.
- [x] **Интеграция:** трейдер — BookFeed(persist=True) + передача в пайплайны; бэкенд — BookFeed(persist=False) + `GET /api/book/status`; UI — ячейка «BOOK GATE (fail_open)» с фичами последнего бара. **+15 тестов** (`realtime/tests/test_book_feed.py`).
- [ ] **Известная проблема:** бэкендский book-feed не получает снэпшоты, пока трейдер держит DOM-подписку (зависит от тайминга старта процессов) — статус `subscribed=false`/0 снэпшотов при живом сборе в трейдере. Косметика: на сигналы не влияет (fail-open), источник данных — CSV трейдера. Fix: единый poller в трейдере + прокси статуса через `/api/book/status` — будущая работа.
- [ ] **Phase 2 — обучение/валидация на book-фичах:** накопить бары в `data/book_bars/BITCOIN.csv` (сейчас пишутся вживую), добавить `imb5_last/imb5_mean/walls_max/spread_mean/microprice_last` в `FEATURE_COLUMNS`, переобучить BTC-модель, перезамер бэктеста.
- [ ] **Phase 3 — настройка порогов гейта** по собранной статистике (частота ложных вето/бустов, sensitivity к veto_imbalance/boost_confidence).

## 4. Чек-лист проверки и эксплуатации

- [x] **Тестовый набор:** `pytest -q` — **908 passed, 232 warnings** (2026-08-19; +15 book-feed, +2 trial-state-aware fixes, observer-proxy readiness fix, +7 3-LEG breakeven regression tests, +2 TTL-cache single-flight/stale tests, +5 group-aware risk budget tests, +6 trading-blackout tests; warnings: малые synthetic CSCV fixtures и Starlette deprecation). Исторические counts в change log не являются текущим статусом.
- [x] **Trading blackout (запрос владельца 2026-08-19):** `execution.trading_blackout` в конфиге (окна UTC): daily break 21:00–22:00 (пропуск новых входов, позиции не трогает), weekend-окно Пт 21:00 → Вс 21:00 UTC (флэт + полный halt, включает 24/7 BTC), ручной halt `manual_halt_until_utc: 2026-08-24 07:00` (владелец в отъезде до конца недели). Трейдер в halt закрывает все позиции магика (best-effort market close, label blackout-halt), логирует причину и время возобновления, автоматически выходит из окна. Гард дублирован в `execute_signal`. Проверено live: 4 позиции закрыты, трейдер в halt, логи не растут.
- [x] **Риск-бюджет по группам (запрос владельца 2026-08-19):** `execution/risk_manager.py::can_trade` принимает `groups_by_asset`/`singles_by_asset` от `mt5_trader._group_position_counts` — 3-ногая группа занимает ОДИН слот. Лимит 6/6 больше не душит сделки: ранее 6 слотов = 2 актива (сигналы XAGUSD conf 0.88/0.97 были подавлены), теперь 6 групп = все 5 активов. Подключён `max_open_positions_per_asset: 2` (групп на актив; был объявлен, но не работал). Legacy-путь без group-инфо считает позиции как раньше.
- [x] **Пороги гейта ослаблены (запрос владельца 2026-08-19, trial):** XAUUSD 0.71→0.60, XAGUSD 0.75→0.65, BTCUSD 0.68→0.62 (+ml_confidence_floor 0.68→0.60, min_ml_probability 0.60→0.55, crypto_night_min_probability 0.62→0.60, ev_threshold 0.05→0), EURUSD 0.85→0.70, GBPUSD 0.60→0.55. Session/regime suppression и корреляционный фильтр (0.80) сохранены.
- [x] **Проп-челлендж (HashHedge «US STOCKS HEADLINERS», запрос владельца 2026-08-19):** новый пакет `challenge/` — веб-терминал (НЕ MT5), аккаунт $1,000, цель +$80, дневная потеря -$50, общая -$100, плечо 1:5, мин 5 дней, сессия NYSE 18:30-00:55 местного. Модули: `browser` (persistent-профиль, ручной логин без хранения кредов), `login`, `explore` (дамп DOM/скриншоты), `connector` (HashHedge adapter), `risk` (полулимитные подушки: дневной стоп -$25, общий -$60, лок прибыли +$20, сайзинг по риску $5/сделку с кэпом 1:5), `strategy` (opening-range breakout 30 мин), `runner` (цикл: снэпшот → риск → управление stop/tp → входы → флэт 00:45). Конфиг `challenge:` в config.yaml, state в data/challenge_state.json, сделки в data/challenge_trades.csv. 15 тестов (windows/risk/strategy). Сеть: TLS-блокировка снята (Python/Chromium работают), SnowVPN активен.
- [x] **Проп-челлендж: разведка терминала (2026-08-20 ночь):** терминал — отдельное UTEX-приложение `markets-app.hashhedge.com/stocks-usdt` (токенизированные US-акции в парах с USDT, 11,775 тикеров). Логин-детекция переведена с URL на DOM-маркеры (SPA рендерит форму логина даже на URL дашборда — 3 ложных «LOGGED IN» до фикса). Пользователь вошёл вручную; сессия в профиле переживает рестарт браузера. Разведка: страница счётов → кнопка «Торговать» открывает новую вкладку с терминалом (`exchange-pro/<SYM>-USDT?modal=ticker&...&session=<uid>`). Смаплены стабильные селекторы: balance-chip («1 000 $ / 0 $ прибыль / Свободно для торговли с плечом 5 000 $»), вкладки `terminalTabPositions/OpenOrders/History/Executions`, форма ордера `input[name=qty/price/total]`, кнопки «Купить/Продать <SYM>, мкт.», «Закрыть позицию», «Принять»; дашборд `stocks-usdt/dashboard` (Мои позиции / Открытые заявки / Закрытые). session_id 770216147 → config. `HashHedgeConnector` реализован (snapshot/quote/place_order/close_position/flatten/balance), runner переключён на него; watchlist 20 ликвидных тикеров. Дальше: проверить исполнение ордера (после старта челленджа), замапить строки позиций, прогон в сессию 18:30-00:55.
- [x] **Дашборд (клики):** TTL-кэш `_ttl_cache` в `realtime/app.py` переведён на single-flight + stale-while-revalidate — параллельные опросы `/api/matrix` больше не запускают повторные ~40s-пересчёты (дашборд «замерзал»); 2 регрессионных теста.
- [x] **Новости (аудит 2026-08-19):** FF/Cloudflare блокирует TLS этой машины (см. ниже про сетевую блокировку). `news_feed_browser_worker.py`: goto 25s→60s + retry-пауза + relaunch; `news_feed_server.py`: при сбое воркера отдаёт последний успешный календарь (200 + `stale`/`stale_seconds`) вместо 502. Внимание: блокировка не-брокерского TLS (OpenSSL/BoringSSL: python, node, curl, Playwright Chromium) затрагивает и Telegram-поллинг трейдера — проверить VPN/фаервол/антивирус/роутер; MT5 и Schannel (PowerShell) работают.
- [x] **Веб-дашборд:** код/API реализованы; фактический deployment status определяется `/health`, а каждое значение обязано показывать `source/mode/as_of` — постоянный ONLINE здесь не утверждается.
- [x] **API эндпоинты:** `/health`, `/signal`, `/api/matrix`, `/api/correlation`, `/api/paper-status`, `/api/status`, `/api/sentiment`, `/api/monte-carlo`, `/api/chart/XAUUSD` — **Все работают**.
- [x] **Симуляция LOB:** `python -m scripts.run_simulation` — **Проверено**.
- [x] **Ночной таймер:** `deploy/overnight/overnight.timer` — **Сконфигурирован**.
