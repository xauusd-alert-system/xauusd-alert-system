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
