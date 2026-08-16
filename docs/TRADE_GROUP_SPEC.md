# TradeGroupSpec v1 — статус реализации

**Дата:** 2026-08-16. **Документ-источник:** ТЗ «TradeGroupSpec v1 — нормализованная сделка TP1/TP2/TP3, риск, BE и MT5 execution».
**Режим rollout:** только paper/demo; live-исполнение запрещено до отдельного явного подтверждения (ТЗ §29/§32).

> Финансовое предупреждение: техническая спецификация программной системы, а не
> инвестиционная рекомендация. Новый execution path в live НЕ включён.

## 1. Что реализовано (P0 полностью, P1 частично)

| Раздел ТЗ | Статус | Где |
|---|---|---|
| **§5/§24 — domain-контракт** `TradeGroupSpec v1`, `TradeLeg`, `GroupState`, `BreakEvenPolicy`, `GroupRisk` | ✅ | `execution/trade_group.py` |
| **§12 — immutability geometry** | ✅ | pydantic frozen; `with_actual_fill()` меняет только `entry.actualFill`; `geometry_hash()` стабилен |
| **§14 — group risk (один раз на группу)** | ✅ | `check_group_risk` (cash + pct); никогда 3× риск |
| **§15 — volume allocator** | ✅ | `allocate_leg_volumes`: floor leg1 → floor leg2 → leg3 = остаток; `INSUFFICIENT_VOLUME_FOR_THREE_LEGS` |
| **§7/§10 — geometry engine** | ✅ | `execution/trade_geometry.py` — pure, без MT5: профили, step, TP/SL, tick alignment, broker min distance, cost admissibility, R, reason codes |
| **§8/§9 — profiles** | ✅ | `config.trade_profiles`: `xau_m15_intraday_v1` (validated), `btc_m5_scalp_v1` (validated:false, paper-only кандидат). Live BTC signal_grid (20–50) НЕ тронут |
| **§17/§18 — BE calculation** | ✅ | raw = actual fill; protected = fill + spread + exit slippage + commission; tick-align away from market; BE_CONFIRMED только после modify+query |
| **§19–§22 — Telegram formatter** | ✅ | `alerts/formatter.py`: final geometry authoritative; recomputation запрещена для `trade-group.v1`; legacy fallback только для старых сигналов; lifecycle update формат |
| **§20 — parity** | ✅ | `spec.as_geometry_payload()` == Telegram == ledger payload (тест) |
| **§23 — state machine** | ✅ | DRAFT→VALIDATED→SUBMITTED→OPENED→TP1_FILLED→BE_REQUESTED→BE_CONFIRMED→TP2_FILLED→TP3_FILLED→RECONCILED; terminal: STOPPED/REJECTED/EXPIRED/CANCELLED/FAILED; BE_RETRY |
| **§25 — persistence** | ✅ | `data/trade_group_store.py`: spec/state/legs/BE/broker ids; `try_mark_submitted` — защита от duplicate orders при restart |
| **§26 — ledger lifecycle events** | ✅ | `data/trading_event_ledger.py`: 15 новых event types + nullable `group_id`/`leg_id` колонки (in-place миграция) |
| **§13 — account mode / constraints** | ✅ | `execution/broker_adapter.py`: `get_account_mode()` (hedging/netting/unknown), `get_symbol_constraints()` (tick, point, digits, stops/freeze, spread, contract, volume grid, exec mode) |
| **§27 — trade_logger** | ✅ | `data/trade_logger.py`: nullable group-колонки (non-destructive) |
| **§29 — paper lifecycle executor** | ✅ (paper) | `execution/trade_group_executor.py`: PaperDriver (netting/hedging), simulate_tick, BE retry, restart recovery; demo — по `TRADE_GROUP_ENABLE_DEMO=1`; live — всегда `LiveExecutionForbidden` |
| **§28 — тесты** | ✅ | 57 новых тестов (см. ниже) |
| **§27 — `realtime/pipeline.py` split** | 🟡 | Прямого рефакторинга pipeline нет (live path не трогаем); мост `build_trade_group_from_signal()` в `trade_geometry.py` потребляет ML-сигнал и выдаёт validated spec |
| **§27 — `mt5_trader.py` group execution** | ⏳ | НЕ менялся. Current live path = unchanged по ТЗ. Hedging/netting submit в live — отдельный P1.5 после acceptance |

## 2. Reason codes (ТЗ §10)

`TP1_TOO_CLOSE_TO_COST`, `STOP_BELOW_BROKER_MIN_DISTANCE`, `INVALID_TICK_ALIGNMENT`,
`PROFILE_NOT_VALIDATED`, `SIGNAL_EXPIRED`, `RISK_LIMIT_EXCEEDED`,
`INSUFFICIENT_VOLUME_FOR_THREE_LEGS`.

Система НИКОГДА не растягивает TP/SL до произвольных 20/50/100 units: невалидный
уровень → `GeometryRejected(reason_code)` → `signal_rejected`.

## 3. Единицы (ТЗ §4)

* `price_distance` = `abs(price_a - price_b)` — стратегическая геометрия в price units.
* `terminal_points` = `price_distance / SYMBOL_POINT` — только broker diagnostics
  (`execution/trade_geometry.py::terminal_points`).
* Никаких предположений «4 пункта одинаковы для BTC и золота».

## 4. BTC: обязательное исправление (ТЗ §9)

- Текущий live-профиль BTC (`signal_grid.step_min_points: 20–50`) **не изменён**.
- `btc_m5_scalp_v1` — кандидат: `validated: false`, `validation_status:
  pending_btc_validation`, `paper_only: true`. Validation gate возвращает
  `PROFILE_NOT_VALIDATED` — approved group из него невозможен.
- Значения кандидата (min 4.0 / max 6.0 price units) — НЕ копия XAU; перед любым
  промоушеном обязателен frozen-data validation gate (ТЗ §30): signal count, hit
  rates, gross/net expectancy, cost ratio, drawdown, sensitivity, tail.

## 5. Пример end-to-end (paper)

```text
ML signal (bias/confidence/regime/atr/model metadata)
  → build_trade_group_from_signal()          # профиль + cost/broker snapshot
  → TradeGroupSpec (immutable geometry)      # TG-20260816-...
  → executor.create_group()  = VALIDATED
  → executor.submit_group()  = SUBMITTED     # netting: 1 позиция, legs 2/3 virtual
  → simulate_tick(TP1)       = TP1_FILLED    # actualFill прикреплён
  → request_break_even()     = BE_REQUESTED  # BE от actual fill
  → confirm_break_even()     = BE_CONFIRMED  # только после modify + broker query
  → simulate_tick(TP2/TP3)   = TP2_FILLED → TP3_FILLED → RECONCILED
  → Telegram: format_trade_group_message() == ledger geometry (parity)
```

## 6. Тесты (57 новых)

| Файл | Покрывает |
|---|---|
| `execution/tests/test_trade_group.py` | contract, immutability, state machine, volume allocation, group risk, ids, R, BE |
| `execution/tests/test_trade_geometry.py` | профили, gate, step, geometry, cost/broker/risk/volume rejection, parity build |
| `execution/tests/test_trade_group_executor.py` | paper lifecycle, BE rejection/retry, query mismatch, stop, restart без дублей, hedging/netting, mode gates, actual-fill BE |
| `data/tests/test_trade_group_store.py` | roundtrip, state update, submitted guard, listing |
| `alerts/tests/test_formatter_trade_group.py` | layout, no-recomputation, formatter_error, legacy fallback, lifecycle update, parity |
| `execution/tests/test_broker_adapter.py` | account mode, symbol constraints |

## 7. Что НЕ сделано (явно отложено)

- `mt5_trader.py` group execution (hedging/netting submit в live/demo MT5) — P1.5,
  отдельным коммитом, после acceptance; current live path неизменен.
- `realtime/pipeline.py` глубокий рефакторинг (разделение ML и geometry) — мост уже
  существует; полный split — вместе с mt5_trader интеграцией.
- BTC profile validation на frozen data (ТЗ §30) — P2.
- Live promotion — запрещено без отдельного подтверждения пользователя (профиль,
  account mode, risk cap, validation results, rollback plan).

## 8. Явные запреты (ТЗ §33) — соблюдены

Не менялась live BTC конфигурация; нет hardcoded TP/SL 4–6 для всех активов; ML не
задаёт TP/SL; TP2/TP3 не меняются после TP1; geometry не пересчитывается после
открытия; TP/SL не растягиваются при rejection; BE считается от actual fill;
риск считается один раз на группу; BE_CONFIRMED невозможен без broker confirmation;
netting не представлен как 3 независимые позиции; новый режим не включён в live;
существующие regression tests не удалены; risk guards не отключены.

---

## 9. Follow-up (2026-08-16): исправления и усиление гарантий

Коммит `fix: harden TradeGroupSpec direction, BE, and paper execution invariants`
(поверх `b35d335`). `execution/mt5_trader.py` и live-конфигурация BTC **не тронуты**.

### Исправления кода

| Пункт follow-up ТЗ | Исправление |
|---|---|
| §2 SHORT SL validation bug | Знаковая формула заменена на явные direction-цепочки: LONG `SL < entry < TP1 < TP2 < TP3`, SHORT `TP3 < TP2 < TP1 < entry < SL` (`execution/trade_group.py::validate_contract`) |
| §16 BE применяется ко всем остаточным legs | `confirm_break_even()` теперь модифицирует и проверяет **каждый** ref в `break_even.apply_to` (раньше только `apply_to[0]`); netting-драйвер резолвит virtual legs на агрегированную позицию (`execution/trade_group_executor.py`) |
| §9 demo env gate | `TradeGroupExecutor(allow_demo=None)` читает `TRADE_GROUP_ENABLE_DEMO` (fail-closed: unset/0 → demo blocked) |
| §15 единый parity helper | `format_trade_group_message()` строит уровни из `spec.as_geometry_payload()` — единственный источник для Telegram/execution/ledger |

### Новые тесты (+58, всего 737 passed)

- **§3 direction regression**: valid/invalid LONG ×2, valid/invalid SHORT ×4 (entry 100 / TP 104/108/112 / SL 90 и зеркально).
- **§4 geometry engine direction**: LONG/SHORT цепочки + симметрия расстояний одного профиля.
- **§5 BE direction**: LONG protected > raw, SHORT protected < raw; parametrized spread/slippage/commission/tick_size; tick alignment.
- **§6 immutability**: ATR/spread/new-candle изменения дают другой *кандидат*, но approved spec (`as_geometry_payload()`) не меняется.
- **§7 write-once fill**: None→100.05 allowed; 100.05→100.05 idempotent; 100.05→100.10 rejected.
- **§8 allocation**: 0.03/0.04/0.05/0.10 → sum(legs)==total; direction-independent.
- **§9 risk symmetry**: LONG и SHORT с одинаковыми distance/volume → одинаковый estimated loss (abs).
- **§10 forbidden live**: env=0+live → `LiveExecutionForbidden`; env=1+demo → allowed; **paper lifecycle → 0 вызовов `order_send`** (spy driver).
- **§11 mt5_trader guard**: исходник `mt5_trader.py` не содержит ссылок на group executor / `TRADE_GROUP_ENABLE_DEMO`.
- **§12 BTC gate**: `config.yaml` BTC live 20–50 не изменён; `btc_m5_scalp_v1.validated=false` + `PROFILE_NOT_VALIDATED`.
- **§13 SHORT Telegram parity**: entry 100 / TP1 96 / TP2 92 / TP3 88 / SL 110 — без зеркалирования/пересборки/legacy step; порядок TP сохранён.
- **§14 missing geometry per-field**: tp1/tp2/tp3/sl/entry.reference → `formatter_error` (никакого fallback).
- **§16/§17 hedging lifecycle**: 3 физических legs; после TP1 leg1 CLOSED, leg2/leg3 SL→BE (query подтверждает), TP2/TP3 immutable.
- **§18 no premature BE**: price 102 < TP1 104 → нет событий, `BE_REQUESTED` недостижим до `TP1_FILLED`.
- **§19 BE retry→success**: rejection → BE_RETRY (без BE_CONFIRMED) → после восстановления broker → BE_CONFIRMED.
- **§20/§21 restart recovery matrix**: OPENED/TP1_FILLED/BE_REQUESTED/BE_RETRY/BE_CONFIRMED/TP2_FILLED — state/geometry/broker ids восстановлены, duplicate orders/TP/BE событий нет, ledger chronological + dedup (ровно 1 событие каждого типа).

### Результат тестового запуска (фактический, текущий workspace)

```text
BASELINE: 679 passed, 11 warnings   (commit b35d335)
AFTER FIX: 737 passed, 11 warnings  (текущий commit)
NEW TESTS: 58
WARNINGS: 11 (известные: Starlette deprecation, малые synthetic CSCV fixtures)
```

---

## 10. P1.5 — demo MT5 TradeGroup execution (2026-08-16)

Коммит `feat: add demo MT5 TradeGroup execution and reconciliation` (поверх
`eb00883`). LIVE запрещён; `execution/mt5_trader.py` и live BTC 20–50 не тронуты.

### Новые модули

| Модуль | Назначение (ТЗ P1.5) |
|---|---|
| `execution/execution_intent.py` | Immutable `ExecutionIntent` — ссылается на `TradeGroupSpec.geometry_hash()`; `require_geometry_unchanged()` перед submission |
| `execution/mt5_common.py` | `MT5BrokerContext`: свежий broker snapshot (никакого stale-cache), `account_mode()` (hedging/netting/unknown — never guessed), tick-align-only normalization, `ORDER_GEOMETRY_INVALID` вместо растягивания TP/SL, comment `TG:<gid>\|L:<leg>\|<magic>` |
| `execution/mt5_hedging_adapter.py` | 3 физических позиции: leg1→TP1, leg2→TP2, leg3→TP3, общий SL; magic/comment корреляция |
| `execution/mt5_netting_adapter.py` | 1 aggregate position (total volume, SL=spec.sl, TP=tp3) + 3 virtual legs; partial close по TP; **один** SL modify для virtual legs (никаких противоположных модификаций) |
| `execution/mt5_trade_group.py` | `MT5TradeGroupExecutor` (demo): submit (demo-account gate + account-mode detect + constraint validation + risk/TTL re-check + intent verification) и `poll_once()` — reconciliation-driven state machine |
| `execution/reconciliation.py` | `inspect_group` (deals по ВСЕМ known tickets, включая закрытые позиции), `classify_broker_close` (tp1/tp2/tp3/stop по immutable spec), `detect_orphan_positions` (ORPHAN_BROKER_POSITION, idempotent, не управляется автоматически), volume-mismatch sync |
| `data/trade_group_store.py` | + таблица `trade_group_actions` (actionId PK, `mark_action` idempotent), колонки `intent_json`/`account_mode` |
| `data/trading_event_ledger.py` | + `leg_partially_filled`, `group_opened`, `orphan_broker_position`, `execution_error` |

### Гарантии (проверены тестами)

* **LIVE всегда заблокирован** (`LiveExecutionForbidden`); demo — только при
  `TRADE_GROUP_ENABLE_DEMO=1` + `account.trade_mode == DEMO` (`DemoAccountRequired`);
  account mode `unknown` → `ACCOUNT_MODE_UNKNOWN` (никогда не угадывается).
* **TP1/TP2/TP3/SL immutable**: broker-закрытие классифицируется по immutable spec;
  после TP1 меняется только SL остатка (BE), TP2/TP3 не трогаются ни в state, ни в
  broker-запросах.
* **TP1/BE только по broker confirmation**: локальная цена — только candidate
  trigger для netting partial close; состояние меняется по результату
  order/deal/position query. Deals «потребляются» один раз (actionId `DEAL-<ticket>`).
* **BE**: от actual fill; все required legs (apply_to) модифицируются и проверяются
  query'ем; rejection → `BE_RETRY` (bounded) → FAILED, никогда `BE_CONFIRMED`;
  restart-safe (query-first, mark-on-success).
* **Idempotency**: `OPEN-L1..3`, `CLOSE-TP1/2/3`, `BE-L2/L3`/`BE`, `DEAL-<t>` —
  повтор actionId не создаёт duplicate action; restart не re-send'ит entry/close/BE.
* **Partial fill**: `leg_partially_filled` + `PARTIALLY_FILLED`; remainder
  cancel-политика (детерминированная), никаких top-up ордеров.
* **Rejected leg** при открытии → `leg_rejected` + controlled recovery: group FAILED,
  принятые legs сохраняют broker ids (не становятся silent orphans).
* **Orphan**: позиция с нашим magic без local group → `ORPHAN_BROKER_POSITION`
  (idempotent по ticket), не управляется.
* **Telegram**: сообщения только после broker-confirmed событий; формат §35
  (OPENED/TP1/BE/TP2/TP3/STOPPED, `Mode: DEMO`, group id).

### Тесты (фактический запуск)

```text
Base commit: eb00883
New commit:  (P1.5, см. git log)
Tests:
  BASELINE: 737 passed, 11 warnings
  AFTER:    772 passed, 11 warnings
  NEW:      35 (34 demo-executor + 4 reconciliation; всего в execution/tests: 168)
```

Покрытие §42–§46: сценарии A–F (hedging long/short, netting full cycle, stop,
BE rejection→retry, restart после TP1), partial fill, order reject, restart
after open/tp1/during-be-retry, idempotent action, orphan detection, no
duplicate orders после reconciliation, telegram после confirmation, BTC
unvalidated profile blocked, broker unavailable (no false success), BE retries
exhausted → FAILED.

### Demo account smoke test

**Реальный Windows/MT5 demo smoke test в этом sandbox НЕ проводился** (нет
Windows-терминала и пакета MetaTrader5). Вместо него все demo-сценарии прогнаны
на детерминированном `FakeMT5`-двойнике, зеркалящем реальную поверхность
MetaTrader5 Python API (account_info / symbol_info / symbol_info_tick /
order_send / positions_get / history_deals_get + константы). Адаптеры написаны
против этой поверхности без изменений для реального терминала; обязательный
реальный demo smoke test (broker symbol, account mode, ticket/order/deal IDs)
выполняется на Windows-хосте по чек-листу §48.
