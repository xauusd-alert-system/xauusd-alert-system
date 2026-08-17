# WEB UI HONESTY AUDIT — старый FastAPI dashboard против спецификации

**Дата:** 2026-08-16. **Документ-источник:** «Полная реализация личного веб-интерфейса торговой системы» (спецификация, §12 и §6.3).

Этот документ фиксирует, как требования спецификации закрыты **в этом репозитории**
(`xauusd-alert-system`). Сам Signal Desk (React + tRPC + MySQL/TiDB) живёт в отдельном
проекте `trading-system-playbook`, которого нет в этом sandbox; здесь он не менялся.

**Стратегия (по спецификации):** старый FastAPI dashboard не ремонтируется как главный
продукт — он остаётся внутренним compatibility/API-слоем. Его synthetic fallbacks
удалены, mutation controls отключены, WebSocket заменён на ledger event stream.

## 1. Статус исправлений по таблице §12

| Старый элемент | Требование спецификации | Статус в репо |
|---|---|---|
| `DATA_MODE=mock` | Показывать режим/source banner; live endpoint без MT5 → `unavailable`, не mock-числа | ✅ Уже было: все endpoints при `DATA_MODE != live` возвращают `available=false`/503 без чисел. Добавлен header-баннер `INTERNAL DIAGNOSTIC VIEW` в dashboard. |
| `/api/status` fallback balance | `available=false, balance=null, equity=null` | ✅ Уже было; добавлены `freshness_status=offline` + `as_of_utc_ms`/`ingest_lag_ms`/`last_successful_at_utc_ms`. |
| `/api/matrix` neutral fallback | per-asset `status=error` с причиной; не создавать neutral confidence | ✅ **Исправлено в этом заходе**: ветка исключения теперь `bias=None, confidence=None, status=error, reason=...`; ветка non-live — `status=unavailable`. Никаких `neutral`/`0.50`. |
| `/api/correlation` fixed matrix | Удалить или считать из реальных synchronized closes | ✅ Уже было: реальные close-returns с as-of/coverage-гейтами; добавлен `freshness_status`. |
| `/api/sentiment` sample headlines | Реальный источник либо `unavailable` | ✅ Уже было: `available=false, reason=no_live_news_source_configured`; добавлен `freshness_status=waiting`. |
| `/api/monte-carlo` fixed P&L | Читать verified closed-trade ledger; `insufficient_data` | ✅ Уже было: `trading_events.position_closed.realized_pnl` + `verify_event_chain`; `at_least_two_closed_trades_required`. Добавлен `freshness_status`. |
| `/api/chart/{asset}` random candles | Реальные MT5/SQLite OHLCV + source metadata | ✅ Уже было: реальные closed candles, 503 без live-режима, заголовки `X-Data-Source`/`X-As-Of-UTC`. |
| `/api/institutional-metrics` fixed metrics | Удалить synthetic или считать из доказуемых фич | ✅ Уже было: считается из реальных closed candles; `available=false` иначе. |
| `/api/positions` empty stub | Реальные MT5 positions read-only + source/as-of | ✅ Уже было: `mt5_positions`/`unavailable`; добавлен `freshness_status=offline` при недоступном MT5. |
| `/api/control/*` | Закрыть auth или удалить до command bus | ✅ **Исправлено в этом заходе**: все действия (`pause`/`resume`/`closeall`/любые) возвращают `501` даже с валидным токеном; без токена — `403`. Пока нет command bus (idempotency/confirmation/kill-switch/reconciliation), браузерных mutation controls не существует. Единственное управление исполнением — авторизованный Telegram-бот. |
| `/ws` echo | Ledger event stream либо polling | ✅ **Исправлено в этом заходе**: `/ws?token=<owner>` — owner-only push-stream нормализованных `ledger_events` (snapshot + инкрементальные push каждые 2s + heartbeat/freshness). Без токена — `UNAUTHORIZED` + close 1008. Дубликаты невозможны (строгий курсор по `received_at_utc_ms`). |
| External CDN Tailwind/FontAwesome | Не зависеть от CDN для критичного экрана | 🟡 Принято: страница — диагностический compatibility view (не live-терминал). Продакшн-UI — Signal Desk (другой проект, свой стек). CDN-зависимость документирована и допустима только для этой страницы. |

## 2. Контракт свежести (DataEnvelope)

Модуль `realtime/data_envelope.py` реализует контракт спецификации §6.3:

| Статус | Условие (по умолчанию) |
|---|---|
| `fresh` | последнее наблюдение ≤ 5 с назад |
| `stale` | 5–60 с |
| `offline` | > 60 с **или** producer ожидается, но недоступен (MT5 не подключён) |
| `waiting` | наблюдений ещё не было (пустой ledger, нет sample) |
| `error` | явная ошибка producer/schema/DB — старое значение не выдаётся как текущее |

Ключи: `source`, `mode`, `as_of_utc_ms`, `freshness_status`, `ingest_lag_ms`,
`coverage`, `last_successful_at_utc_ms`. (В этом репо snake_case; в Signal Desk —
camelCase; маппинг тривиален.)

Правило, закреплённое тестами: недоступный источник **никогда** не превращается в
числовой fallback — нет `$100,000`, нет нейтрального `0.50`, нет случайного графика.

## 3. WebSocket-контракт (`/ws`)

```
клиент ── GET /ws?token=<LEDGER_OWNER_TOKEN|LEDGER_INGEST_TOKEN> ──► сервер
сервер ── {"type":"events","count":N,"events":[...],"as_of_utc_ms":...,
           "freshness_status":...,"server_time_utc_ms":...,
           "deployment_mode":...,"data_mode":...} ──► клиент (каждые 2 с)
```

- Без токена или с неверным токеном: `{"type":"error","code":"UNAUTHORIZED"}` + close 1008.
- Команды от клиента не принимаются (read-only stream).
- Курсор строгий (`since_ms = max(received)+1`), повторная отправка исключена.
- Для работы через uvicorn требуется `websockets`/`wsproto` (`uvicorn[standard]`).

## 4. Тесты

- `realtime/tests/test_data_envelope.py` — границы fresh/stale/offline/waiting/error.
- `realtime/tests/test_dashboard_honesty.py` — no-fallback матрицы, status, positions,
  correlation/sentiment, monte-carlo, отсутствие control-кнопок в HTML.
- `realtime/tests/test_ledger_ingest.py` — WS auth (1008), WS stream + отсутствие
  дубликатов, freshness-ключи у ledger endpoints.
- `realtime/tests/test_app.py::test_api_control_endpoints_are_disabled` — 403 без токена,
  501 для всех действий с токеном, `trading_paused` не меняется.

## 5. Что осталось (вне этого репо)

- Signal Desk (React/tRPC/MySQL) — проект `trading-system-playbook`: страницы
  Overview/Signals/Lifecycle/Positions/Execution Quality/Risk/Research/System Health.
- Реальная доставка событий с Windows-хоста в Signal Desk (P0) — MQL5 observer +
  Python bridge уже готовы в этом репо (`mql5/SignalDeskObserver/`,
  `data/ledger_bridge.py`, `POST /api/ledger/ingest`).
- Wave 3+: MT5 account/positions/deals read-only views на Windows-хосте, acceptance
  checklist, empirical cost dataset.
