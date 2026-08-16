# LEDGER_BRIDGE — MT5 observer producer и серверный event ledger

**Статус:** Wave 2 плана `docs/MQL5_OBSERVER_PLAN.md` (2026-08-16).

Этот документ описывает, как broker execution facts попадают из MT5-терминала в
серверный append-only ledger, кто какие события производит, как устроены
schema versioning, idempotency и восстановление после сбоев.

## 1. Производители (producers)

| Producer | Источник | События | Точность (`precision`) |
|---|---|---|---|
| Python sender (`execution/mt5_trader.py`) | `SignalIntent` до `order_send` + ответ `order_send` | `intent_created`, `request_result` | `request` |
| MQL5 observer (`mql5/SignalDeskObserver/`) | `OnTradeTransaction` (deal/order/position) | `deal_added`, `order_history_added`, `position_modified`, `execution_reconciled`, `health_heartbeat` | `passive` (live), `history_reconciled` (restart scan) |
| FX probe (`execution/fx_execution_probe.py`) | Отдельный CSV (`logs/fx_execution_probes.csv`) — НЕ пишет в ledger | — | `probe` (schema поддерживает; источник остаётся CSV по дизайну модуля) |
| Preflight tool (будущее) | `OrderCheck`-style проверка | `preflight_checked` | `preflight` |

Сервер нормализует все источники в одну таблицу `ledger_events` (`data/ledger_events.py`).
Второй dashboard/public feed не создаётся: чтения owner-only.

## 2. Поток данных

```text
Python sender                          MQL5 Observer EA (Windows, demo)
  build_signal_intent()                  OnTradeTransaction
  append_signal_intent()                 └─ EventSerializer -> DiskOutbox (jsonl)
  order comment: "<ASSET> ML Scalp <id8>" OnTimer
  order_send --------------------------------> broker (deal/order/position)
  ExecutionEvent(intent_created)          OnTimer FlushOutbox
  ExecutionEvent(request_result)          └─ WebRequest POST (HTTPS + bearer)
        │                                        │
        └──────────────► ledger_outbox (SQLite/WAL) ◄──────┘
                              │ scripts/run_ledger_bridge.py (retry)
                              ▼
              POST /api/ledger/ingest  (HMAC-SHA256 при LEDGER_INGEST_SECRET)
                              ▼
              ledger_events (append-only, PK = event_id)
                              ▼
     GET /api/ledger/events | /execution-quality | /lifecycle/{intent_id}
```

## 3. Контракт событий (schema versioning)

`ExecutionEvent` v1 (`contracts/execution_contracts.py`, `schema_version: 1`):

- обязательные поля: `event_id`, `event_type`, `source`, `account_mode`,
  `broker_symbol`, `precision`, `received_at_utc_ms`;
- опциональные: `intent_id`, `asset_key`, `magic_number`, `order_ticket`,
  `deal_ticket`, `position_ticket`, `deal_time_msc`, `retcode`,
  `requested_price`, `fill_price`, `filled_volume`, `volume_requested`,
  `spread_points`, `commission`, `swap`, `latency_ms`, `reason`, `payload`.

`SignalIntent` v1 (`contracts/execution_contracts.py`): `intent_id`, `asset_key`,
`broker_symbol`, `side`, `requested_volume`, `entry_price`, `sl_price`,
`tp_price`, `model_version`, `feature_manifest_hash`, `config_hash`, `mode`,
`magic_number`, `source`, `signal_id`, `created_at_utc_ms`.

Правило совместимости: новые поля добавляются как optional; `event_id`/`intent_id`
никогда не меняют смысл. При изменении семантики обязательных полей
инкрементируется `schema_version` и добавляется таблица `ledger_events_v{n+1}` —
старые таблицы не переписываются.

## 4. Idempotency и восстановление

- `event_id` детерминирован:
  - Python: `sha256("<source>|<account_fingerprint>|<kind>|<ticket>")`
    (`execution_event_id` в `contracts/execution_contracts.py`);
  - MQL5: та же canonical-строка без хеширования (в MQL5 нет sha256);
    сервер хранит `event_id` как opaque PK, поэтому оба варианта дедуплицируются
    одинаково.
- Outbox (`data/ledger_bridge.py`, таблица `ledger_outbox`): событие считается
  доставленным только после HTTP 2xx (`delivered_at_ms`); при ошибке
  `attempts`/`last_error` обновляются, событие остаётся pending. Повторная
  доставка безопасна (INSERT OR IGNORE по `event_id`).
- MQL5-аналог: `DiskOutbox.mqh` — append-only JSONL + watermark ack
  (`SignalDeskObserver_outbox.ack`); ротация только когда все строки acked.
- Рестарт EA: `HistoryReconciler.mqh` сканирует `HistorySelect(now - N days, now)`
  и эмитит недостающие deal/order факты с `precision=history_reconciled`;
  synthetic fill не создаётся никогда.
- Восстановление Python-моста: `scripts/run_ledger_bridge.py --once` для ручного
  дренажа, или цикл с `--interval`.

## 5. Аутентификация (server side, `realtime/app.py`)

| Endpoint | Требование | Без конфигурации |
|---|---|---|
| `POST /api/ledger/ingest` | `Authorization: Bearer $LEDGER_INGEST_TOKEN`; при `LEDGER_INGEST_SECRET` — ещё `X-Ledger-Signature` (HMAC-SHA256 тела) | 403 fail-closed |
| `GET /api/ledger/events` | `Bearer $LEDGER_OWNER_TOKEN` (fallback: ingest token) | 403 fail-closed |
| `GET /api/ledger/execution-quality` | то же | 403 |
| `GET /api/ledger/lifecycle/{intent_id}` | то же | 403 |

MQL5 observer не умеет HMAC: он полагается на HTTPS + bearer token. Поэтому если в
продакшене используется `LEDGER_INGEST_SECRET`, MQL5-интеграция либо отключает
secret, либо переходит на отдельный endpoint/прокси, который добавляет подпись.

## 6. Схема БД

`ledger_events` (append-only, триггеры запрещают UPDATE/DELETE):

```
event_id TEXT PK | schema_version | source | event_type | intent_id | asset_key |
broker_symbol | magic_number | account_mode | precision | order_ticket |
deal_ticket | position_ticket | deal_time_msc | retcode | requested_price |
fill_price | filled_volume | volume_requested | spread_points | commission |
swap | latency_ms | reason | signature_valid | received_at_utc_ms | payload_json
```

`ledger_intents` (локально у sender и на сервере; append-only, PK `intent_id`).

`ledger_outbox` (локально у sender; `event_id` UNIQUE, `delivered_at_ms`).

`signal_journal` (`logs/journal.py`) — добавлена nullable-колонка `asset_key`
(миграция in-place; legacy rows сохраняются с NULL). Multi-asset rollout не
начинается до заполнения этой колонки.

## 7. Wave 3 — Execution Quality view

`GET /api/ledger/execution-quality` возвращает p50/p90/p95/p99 по `spread_points`,
`latency_ms`, `adverse_slippage_price_units` **раздельно по `precision`**
(probe/passive/history_reconciled никогда не смешиваются в одной серии), плюс
`stale`-флаг: если последняя активность старше 6 часов — UI обязан показывать
`STALE/OFFLINE`, а не историческое значение как live.

Ограничение (честное): passive spread/slippage из `OnTradeTransaction` — это
`approximate`-наблюдения, а не request-to-fill. Точный request-to-fill даёт только
controlled minimum-lot demo probe. Никакой динамический spread-фильтр и никакой
выбор broker'а по одному малому sample до заморозки cost-source версии.
