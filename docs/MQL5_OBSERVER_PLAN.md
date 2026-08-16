# План применения экосистемы MQL5 к `xauusd-alert-system` — статус реализации

**Дата:** 15 августа 2026 (план) / 16 августа 2026 (реализация Waves 0–2 в этом репозитории).

Этот документ фиксирует план (оригинальный текст ниже) и статус его выполнения в
коде. **Ключевое решение:** первый MQL5 EA — только наблюдатель и диагностический
агент в demo mode; он не открывает сделки. Python `execution/mt5_trader.py`
остаётся единственным текущим отправителем ордеров.

---

## Статус реализации (append 2026-08-16)

| Раздел плана | Статус | Где в коде |
|---|---|---|
| **Контракты `SignalIntent v1` / `ExecutionEvent v1`** | ✅ Реализовано + тесты | `contracts/execution_contracts.py`, `contracts/tests/test_execution_contracts.py` |
| **Wave 0 — provenance manifest** | ✅ Реализовано (opt-in) + тесты | `data/provenance.py`, `scripts/build_provenance_manifest.py`, gate в `scripts/train_mt5.py` и `scripts/run_backtest.py` (`validation.require_provenance_manifest`, по умолчанию `false`) |
| **Wave 0 — storage `spread`/`real_volume`** | ✅ Уже было в репо | `data/storage.py` (`OPTIONAL_MARKET_COLUMNS`, COALESCE upsert) |
| **Wave 0 — production `traded` label** | ✅ Уже было в репо | `config/config.yaml` (`assets.XAUUSD.labeling.event: traded`), `scripts/train_mt5.py` |
| **Wave 0 — uniqueness weights** | ✅ Уже было в репо | `model/uniqueness.py`, `model/trainer.py`, metadata в `scripts/train_mt5.py` |
| **Wave 1 — MQL5 Observer EA (read-only)** | ✅ Исходники + wire-contract тесты; компиляция/прогон — на Windows-хосте | `mql5/SignalDeskObserver/` (ObserverEA.mq5, EventSerializer.mqh, DiskOutbox.mqh, SymbolResolver.mqh, HistoryReconciler.mqh, JsonWriter.mqh, README.md), `mql5/tests/test_wire_contract.py` |
| **Wave 1 — `signal_journal.asset_key`** | ✅ Миграция + тесты | `logs/journal.py`, `logs/tests/test_journal_asset.py` |
| **Wave 2 — durable outbox + signed delivery** | ✅ Реализовано + тесты | `data/ledger_bridge.py`, `scripts/run_ledger_bridge.py`, `data/tests/test_ledger_bridge.py` |
| **Wave 2 — серверный ledger `/api/ledger/ingest`** | ✅ Реализовано + тесты | `realtime/app.py` (4 endpoints), `data/ledger_events.py`, `realtime/tests/test_ledger_ingest.py` |
| **Wave 2 — Python sender: intent до `order_send`** | ✅ Реализовано (только на реальном пути отправки; dry-run не менялся) | `execution/mt5_trader.py` (`build_signal_intent` + `append_signal_intent` + `ledger_intents` + short id в comment + `request_result`/`intent_created` facts в outbox) |
| **Wave 2 — `execution_fills` intent/precision** | ✅ Миграция (nullable) | `data/execution_ledger.py` |
| **Wave 3 — эмпирический cost dataset** | 🟡 Каркас готов; сбор фактов — только после acceptance на demo | `GET /api/ledger/execution-quality` (p50/p95, split by precision, stale flag); FX probe остаётся отдельным CSV-источником (`execution/fx_execution_probe.py`) |
| **Wave 4 — acceptance harness / Strategy Tester / Python parity** | ⏳ Требует Windows-хоста MT5; чеклист в `mql5/SignalDeskObserver/README.md` | — |
| **Wave 5 — CUSUM / neural / MTF parity / exclusive gateway** | ⏳ Отложено (plan запрещает до Waves 0–4) | — |
| **Signal Desk UI: Execution Quality / Lifecycle Trace views** | 🟡 API готовы (`/api/ledger/execution-quality`, `/api/ledger/lifecycle/{intent_id}`, `/api/ledger/events`); HTML-views — будущая работа | `realtime/app.py` |
| **`LEDGER_BRIDGE.md`** | ✅ Создан | `docs/LEDGER_BRIDGE.md` |

### Что сделано в этом заходе (кратко)

1. **Контракты.** `SignalIntent` создаётся Python **до** `order_send`,
   персистится в append-only `ledger_intents`; короткий 8-hex id передаётся в
   comment ордера (`"{asset_key} ML Scalp {intent_id[:8]}"`) — correlation-safe
   поле, которое MQL5 observer извлекает в `payload.intent_id_short`.
   `event_id` детерминирован: MQL5 пишет canonical-строку
   `mt5_observer|<fp>|<kind>|<ticket>`, Python — sha256 от той же строки;
   серверный upsert идемпотентен по `event_id`.
2. **MQL5 Observer EA** (`mql5/SignalDeskObserver/`): `OnTradeTransaction` пишет
   только в файловый outbox (без сети), `WebRequest` — только из `OnTimer`,
   demo/contest-only (`INIT_FAILED` на real), fail-closed symbol resolver,
   история-реконсиляция после рестарта с `precision=history_reconciled`.
   Компиляция и acceptance-прогон — на Windows-хосте (см. README модуля).
3. **Ledger bridge**: `ledger_outbox` (SQLite/WAL, append-only, ack-state),
   HMAC-SHA256 подпись (Python; MQL5 — HTTPS+bearer), retry без потери событий.
4. **Сервер**: `POST /api/ledger/ingest` (bearer + опциональная подпись,
   идемпотентный upsert в append-only `ledger_events`), owner-only чтения
   `events` / `execution-quality` / `lifecycle/{intent_id}`; stale-флаг для UI.
5. **Provenance**: immutable manifest (broker, terminal hash, symbol mapping,
   timezone offset, source window, per-year/per-session gap audit, data hash);
   `verify`/`gate` fail-closed; CLI `scripts/build_provenance_manifest.py`.

### Явные запреты (повтор из плана)

Не включать real-money orders, не запускать активный probe на реальном счёте,
не копировать CodeBase strategy/EAs, не добавлять Transformer/Mamba/RL/LLM, не
импортировать unreviewed DLL, не менять thresholds по locked/live-forward data и
не превращать observed passive fill-vs-quote в точный request-to-fill slippage.

---

## Оригинальный план (спецификация)

### Итог в одном абзаце

Следующая ценность для системы — **не новая торговая идея, CodeBase EA или нейросеть**,
а честный слой наблюдаемости и исполнения вокруг уже существующего Python research path.
MQL5 следует использовать на Windows-хосте MT5 как узкий **observer/telemetry agent**,
который фиксирует broker transactions, pre-flight ограничения и фактические costs.
Python остаётся владельцем research/model path; личный Signal Desk — владельцем серверного
event ledger. Такой порядок закрывает P0-разрыв Opus между историческим cost proxy и
фактическим execution, не создаёт второго competing execution engine и не расходует
locked/live-forward данные на настройку.

> **Ключевое решение:** первый MQL5 EA — только наблюдатель и диагностический агент в demo
> mode. Он **не открывает сделки**. Python `mt5_trader.py` остаётся единственным текущим
> отправителем ордеров. Перевод `OrderSend` в MQL5 возможен только в отдельном позднем
> проекте после acceptance tests и явного архитектурного решения.

### Охват исследования и граница полноты

Открыты все пользовательские начальные разделы: `Articles`, `CodeBase`, категории MT5
Libraries/Scripts/Indicators/Experts и категории Articles Machine
Learning/Statistics/Indicators/Trading systems/Trading/Integration. Эти разделы являются
многосотстраничными каталогами с регулярно меняющимся пользовательским контентом. Поэтому
этот отчёт не утверждает, что вручную code-reviewed каждый внешний snippet; вместо этого
он делает **полный обзор всех заданных каталогов** и детальное чтение первоисточников,
прямо относящихся к текущим P0: execution quality, trade-event streaming, unified gateway,
data audit, indicator parity, CUSUM и официальные MT5 semantics.

Внешние CodeBase articles используются как **reference patterns**, а не как поставляемые
зависимости. Перед включением любого внешнего кода требуется license review, dependency
review, isolated tests и собственный implementation PR.

### Что из материалов MQL5 действительно применимо

| Направление | Проверенный вывод из материалов | Применение к вашей системе | Решение |
|---|---|---|---|
| Broker execution quality | Нужно различать request-to-fill measurement и passive post-event estimate; среднее не заменяет p95, tails и entry/exit asymmetry | Собрать spread, retcodes, request/fill prices, latency и факт partial fill для backtest cost scenarios | **P0. Внедрить.** |
| `OnTradeTransaction` | Callback несёт transaction/request/result, но должен оставаться коротким; после событий нужна history reconciliation | MQL5 observer пишет minimal event в local outbox, а не выполняет сеть или стратегию в callback | **P0. Внедрить.** |
| `OrderCheck` и unified gateway | Проверка демонстрирует допустимость/margin, но не доказывает исполнение; gateway должен выдавать structured result, а не `bool` | Создать read-only preflight parity tool и structured `ExecutionEvent`; не заменить текущий Python sender сразу | **P0. Внедрить в режиме diagnostics.** |
| Push event stream / durable delivery | Blocking HTTP внутри transaction callback останавливает event loop; delivery надо вынести в queue + timer | Доставлять deal/order events из disk-backed local outbox в существующий signed `/api/ledger/ingest` | **P0. Внедрить.** |
| Historical data audit | Воспроизводимый backtest требует terminal identity, symbol mapping, coverage/gap audit, immutable cache и serialised MT5 API access | Добавить source/provenance manifest для raw bars перед baseline retraining | **P0. Внедрить.** |
| Indicator-buffer export | Терминальный buffer export устраняет MT5/Python drift только для custom MQL indicators; нужен `BarsCalculated`, timestamp alignment и warm-up policy | Применять только как parity fixture для будущего MQL-specific indicator; не создавать второй training data source | **P1. Ограниченно.** |
| CUSUM | На финансовых данных эмпирические false positives намного выше theoretical prediction; детектор отражает прежде всего volatility break, не direction | Возможный P2 regime/abstention feature c per-instrument calibration | **P2. Отложить.** |
| Strategy Tester/CodeBase EAs | Tester полезен для semantics/acceptance; готовые EAs, grid/recovery logic и optimization results не доказывают alpha | Проверить broker constraints, lifecycle, restart/retry и partial-close semantics; research остаётся в Python | **P1. Дополнить.** |
| Neural/LLM/RL/ONNX/OpenCL | Каталоги богаты такими реализациями, но это новые degrees of freedom до честного data/target/cost baseline | Не включать в следующий релиз | **Не делать сейчас.** |

### Сопоставление с уже созданными планами

| Ранее выявленный пункт | Статус до исследования MQL5 | Как MQL5 материалы его меняют | Обновлённый приоритет |
|---|---|---|---|
| `traded` label не включён в production training | P0 открыт | Не меняют. Никакой EA не исправит target/execution mismatch | **Остаётся P0 №1** (в репо уже закрыт: `labeling.event: traded` + `asset_key`) |
| Uniqueness weights не доходят до production/calibration | P0 открыт | Не меняют | **Остаётся P0 №2** (в репо уже закрыт) |
| `spread`/`real_volume` потеряны в durable candle storage | P0 открыт | Добавляют требования к broker cost/fill collection и source provenance | **P0 №3, расширить** (storage уже мигрирован; cost collection — Waves 1–3) |
| Нет frozen paper accumulator для XAU candidate | P0 открыт | MQL5 observer может быть источником append-only execution events, но не заменяет frozen strategy accumulator | **P0 №4** |
| Signal Desk ledger уже создан | Личный owner-only UI, signed ingest, SQLite poll bridge, no synthetic events | Нужно добавить MT5 `deal/order` lifecycle рядом с существующими `signal_journal` событиями | **P0 integration track** (в репо: `ledger_events` + ingest) |
| CUSUM event sampling | В Opus не реализован | MQL5 data показывают, что CUSUM нужно использовать не как direction alpha | **P2, только regime experiment** |

### Целевая архитектура без двух execution engines

```text
┌─────────────────────────────────────────────────────────┐
│ Python trading system                                     │
│ feature pipeline → model → SignalSpec/intent → mt5_trader │
│ signal_journal (signal_created / intent_created)          │
└───────────────────┬─────────────────────────────────────┘
                    │ existing MT5 Python order path
                    ▼
┌─────────────────────────────────────────────────────────┐
│ MT5 terminal on Windows demo host                         │
│ MQL5 Observer EA (no OrderSend)                           │
│ OnTradeTransaction → append-only local SQLite outbox      │
│ OnTimer → signed HTTPS batch/retry                        │
└───────────────────┬─────────────────────────────────────┘
                    │
       existing SQLite bridge      new MT5 execution bridge
                    │                         │
                    └─────────────┬───────────┘
                                  ▼
┌─────────────────────────────────────────────────────────┐
│ Private Signal Desk / server event ledger                 │
│ signal → intent → preflight → order/deal/position → PnL  │
│ owner-only, as-of UTC, source mode, ingest lag            │
└─────────────────────────────────────────────────────────┘
```

Такой путь предотвращает race condition: MQL5 EA не конкурирует с
`execution/mt5_trader.py` за право отправить ордер. Он лишь фиксирует факт того, что
trade server реально сделал. Позднее можно рассмотреть exclusive MQL5 execution gateway,
но только после доказанного parity и отдельного architectural decision record.

### Контракты, которые нужно зафиксировать до кода

#### `SignalIntent v1`

Python создаёт immutable intent **до** `order_send`, сохраняет его в source journal и
передаёт `intent_id` через correlation-safe поле. Требуемые поля: `intent_id`, `asset_key`,
`broker_symbol`, `side`, `requested_volume`, `entry/sl/tp geometry`, `model_version`,
`feature_manifest_hash`, `config_hash`, `mode`, `created_at_utc_ms`, `magic_number` и
`source`.

#### `ExecutionEvent v1`

MQL5 observer и Python sender публикуют отдельные facts. Минимальные поля: `event_id`,
`event_type`, `intent_id` (если известен), `source`, `account_mode`, `broker_symbol`,
`magic_number`, `order_ticket`, `deal_ticket`, `position_ticket`, `deal_time_msc`, `retcode`,
`requested_price`, `fill_price`, `filled_volume`, `spread_points`, `commission`, `swap`,
`latency_ms`, `precision` (`probe` / `passive` / `history_reconciled`) и
`received_at_utc_ms`.

> `OrderCheck` = **preflight fact**, `OrderSend` response = **request fact**,
> `OnTradeTransaction`/history = **execution fact**. Успех одного не заменяет другой.

#### Idempotency и восстановление

`event_id` строится детерминированно из source + account fingerprint + transaction/deal ID +
transaction type; `intent_id` назначается Python до запроса. Outbox хранит event в
SQLite/WAL до HTTP 2xx acknowledgement. При restart EA запускает reconciliation с
`HistorySelect` и присылает недостающие terminal facts; server upsert делает повторную
доставку безопасной.

### Поэтапный план внедрения

#### Wave 0 — остановить расширение alpha и создать baseline manifest

Параллельно с MQL разработкой не добавлять LSTM, Transformer, RL, Tsetlin, ONNX, CodeBase
signal rules, grid/recovery logic или новый MTF indicator. Сначала выполнить уже
установленные P0 Opus: production `traded` target contract, weighted production
training/calibration и persistence `spread`/`real_volume`.

| Задача | Изменения | Критерий выхода |
|---|---|---|
| Data provenance manifest | `broker`, terminal path/company hash, canonical/broker symbol, timezone, source window, export time, gap report per year/session, data hash | Любой backtest/fit получает immutable source manifest; смешение brokers или incomplete history останавливает run |
| Storage migration | Добавить nullable `spread`, `real_volume`, `source_id`, `ingested_at` в OHLCV; не удалить legacy data | Existing DB migrates non-destructively; backfill state documented; reader сохраняет backward compatibility |
| Production label contract | Передать `asset_key`, добавить versioned `labeling.event`, log class balance and config hash | Legacy vs traded A/B на copy real DB, без изменения locked period |
| Weights/calibration | Shared alignment helper, explicit calibration policy, model artefact metadata | Production/research parity test and no silent unweighted refit |

#### Wave 1 — read-only MQL5 Observer EA

Создать **новый изолированный модуль**, например `mql5/SignalDeskObserver/`, без
`OrderSend`, `CTrade` или trade-modification calls. EA фильтрует account events по
`magic_number` и broker symbol mapping, фиксирует `DEAL_ADD`, `HISTORY_ADD`, `POSITION` и
`REQUEST` facts в local outbox.

| Компонент | Ответственность | Запрет |
|---|---|---|
| `ObserverEA.mq5` | `OnInit`, `OnTradeTransaction`, `OnTimer`, health heartbeat | Не рассчитывает alpha и не открывает/изменяет позицию |
| `EventSerializer.mqh` | Валидирует required fields и serializes JSON | Не читает секреты из chart inputs/logs |
| `DiskOutbox.mqh` | SQLite/file-backed append-only events, ACK state, bounded retention | Не удаляет unacknowledged event |
| `SymbolResolver.mqh` | Canonical `XAUUSD` ↔ broker suffix/prefix, contract metadata snapshot | Не угадывает mapping при неоднозначности |
| `HistoryReconciler.mqh` | На restart проверяет missing transaction/deal sequence | Не создаёт synthetic fill |

**Acceptance checklist.** Run only on a demo account. Verify: no `OrderSend` symbol in
compiled source; every selected demo trade emits terminal event; no double event after
restart; no external network in callback; network outage queues events without blocking;
history reconciliation fills missing callbacks; nonmatching magic events are ignored; all
timestamps are UTC/MSC.

#### Wave 2 — signed delivery into existing ledger

Существующий site endpoint `/api/ledger/ingest` и `ledger_bridge.py` уже принимают
read-only SQLite `signal_journal` events. Не создавать второй dashboard or public feed.
Добавить второй producer с отдельным `source=mt5_observer`; server normalizes both sources
into one append-only ledger.

Сначала использовать `WebRequest` только из `OnTimer` и only against allow-listed HTTPS
URL: секундная latency здесь не критична. Если acceptance tests докажут, что этот путь
мешает terminal behaviour, провести отдельный security review перед optional WinINet DLL.
In-memory queue из статьи не считается достаточной; на restart недоставленные events не
должны исчезать.

| Событие | Producer | Source precision |
|---|---|---|
| `signal_created` / `intent_created` | Current Python journal bridge | Strategy fact |
| `preflight_checked` | Python or separate read-only diagnostic tool | Preflight fact |
| `request_result` | Current Python sender | Request response fact |
| `deal_added`, `order_history_added`, `position_modified` | MQL5 observer | Broker transaction fact |
| `execution_reconciled` | MQL5 history reconciliation | Historical fact |

#### Wave 3 — empirical execution-cost dataset

Собрать append-only real facts on demo before changing any alpha or thresholds. Passive
`OnTradeTransaction` slippage маркируется `approximate`; request-to-fill measurement
получают только из controlled minimum-lot demo probe. Они не смешиваются в одной statistic
series.

| Метрика | Разрез | Использование |
|---|---|---|
| observed spread | asset, session, hour, news flag | Distribution, cost stress input |
| entry/exit slippage | direction, side, `probe/passive` | Separate p50/p95/tail asymmetry |
| request→response latency | terminal/VPS/account mode | Diagnose infrastructure vs market friction |
| retcodes/rejections | symbol, filling mode, session | Preflight/gateway robustness |
| realised commission/swap/partial fills | deal/order/position | Backtest accounting reconciliation |

**Gate:** no dynamic spread filter and no broker selection decision from one small sample.
First publish distributions, missing-data rate and per-session coverage. Reprice historical
validation only after an explicit cost-source version is frozen.

#### Wave 4 — MT5 acceptance harness и Python parity

Использовать Strategy Tester для **execution semantics**, а Python purged
walk-forward/DSR/CSCV для **statistical edge**. Это разные вопросы и один контур не
заменяет другой.

| Test family | What it proves | Pass condition |
|---|---|---|
| Pure MQL unit-like | JSON, schema validation, symbol resolver, idempotency key, missing mapping | Deterministic fixtures pass without market/account |
| Strategy Tester | Stops/freeze levels, filling mode, duplicate event, partial close, transaction order | Exact expected `ExecutionEvent` sequence |
| Demo integration | HTTPS retry, restart, server outage, ledger upsert | 0 lost/duplicate terminal events across planned failures |
| Python parity | Signal intent, config/model hash and historical input provenance | Any mismatch is explicit failure, not fallback |
| Research revalidation | Performance after targets/weights/cost sources change | Purged WF + calibration + DSR/PBO/CSCV + locked process only |

#### Wave 5 — later research, separately gated

После Waves 0–4 допустимы отдельные, preregistered experiments: CUSUM as
volatility/regime abstention feature, MQL-specific indicator buffer parity, compact neural
challenger in Python, or a future exclusive MQL5 gateway. Для CUSUM фиксируются `h/k`,
target instrument, false-alarm budget and train-period calibration **до** OOS; indicator
cannot be marketed as direction predictor.

Модельное усложнение нельзя совмещать с migration costs/label/weights в одном experiment.
Каждая ветка сравнивается с frozen baseline на одинаковых labels, splits, costs и feature
manifest.

### CodeBase shortlist: что взять как reference, а что не брать

| External pattern | Допустимое использование | Не допустимо |
|---|---|---|
| Broker Spec / Preflight Inspector | Design acceptance fixture for volume step, stops, freeze level, filling mode, margin | Импорт без own tests или auto-order side effect |
| Trade Transaction Trace Logger | Event taxonomy and history reconciliation ideas | Treating terminal log as durable server source of truth |
| Execution Cost / Round-Trip tools | Metrics schema and p95/asymmetry calculations | Replacing controlled empirical dataset with its demo data |
| EA Acceptance Harness | Deterministic state-machine tests | Treating synthetic test pass as live execution proof |
| Session/latency monitors | Health and observability fields | Automatic threshold/alpha changes on live data |
| Neural/LLM/RL libraries | None in current release | Native model training/deployment before P0 baseline |
| Recovery/grid/position manager EAs | None in current release | Copying martingale/recovery or conflicting position modification |

### Изменения документации и Signal Desk

Нужно расширить Signal Desk двумя private views без synthetic cards: **Execution Quality**
(as-of, coverage, p50/p95, approximate vs probe) и **Lifecycle Trace** (intent → preflight →
request result → broker transaction → reconciliation). Каждая цифра показывает source, mode
(`demo`/`real`), timezone and data-quality flag. Если MT5 observer offline, UI показывает
`STALE/OFFLINE`, а не historical value как live.

Нужно обновить existing `LEDGER_BRIDGE.md`: добавить MT5 producer alongside SQLite journal,
clear schema versioning and recovery. `signal_journal` currently lacks persisted `asset`, так
что multi-asset rollout не начинается до source schema migration; temporary default asset
mapping не должен пережить expansion.

### Явные запреты следующего релиза

Не включать real-money orders, не запускать active probe на реальном account, не копировать
CodeBase strategy/EAs, не добавлять Transformer/Mamba/RL/LLM, не импортировать unreviewed
DLL, не менять thresholds по locked/live-forward data и не превращать observed passive
fill-vs-quote в точный request-to-fill slippage.

### Порядок работ и зависимости

```text
Target + weights + raw data provenance
        │
        ├──► Demo-only MQL5 observer + durable outbox
        │            │
        │            └──► Signed ledger lifecycle + history reconciliation
        │                         │
        └──► Empirical cost dataset ◄─────────┘
                                     │
                          Frozen paper accumulator
                                     │
                         Full reproducible revalidation
                                     │
                  Only then controlled locked/live-forward read
```
