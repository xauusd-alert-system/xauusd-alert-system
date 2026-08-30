# STRATEGY_VWAP_PULLBACK.md — VWAP Pullback Continuation (signal-only)

Версия: v1 (Stage C baseline). Профиль: `us_stocks_challenge`. Исполнение —
только вручную пользователем; система отправляет Telegram-сигнал.

## Идея

Сильный направленный импульс на объёме → откат к VWAP на затухающем объёме →
подтверждающее закрытие обратно за VWAP со структурой HL/LH → вход на пробое
уровня подтверждающей структуры. Opening Range 15m служит фильтром направления.

## Таймфреймы и данные

- Входные данные: 1m-бары UTEX; внутренняя логика — на закрытых 5m-барах
  (агрегация отбрасывает незакрытый бакет).
- VWAP считается строго от открытия регулярной сессии 09:30 America/New_York,
  типичная цена `(H+L+C)/3`, сброс в новый торговый день. Премаркет-бары (<09:30 NY)
  автоматически отфильтровываются и не искажают дневной VWAP.
- OR15 = первые **три полностью закрытые** 5m-свечи регулярной сессии (09:30–09:45).
- Поддержка дней раннего закрытия рынка (Early Close Days, 13:00 NY) через `NySession`.

## Чеклист LONG (все пункты обязательны)

| Код | Правило |
|---|---|
| `WATCHLIST_MEMBER` | тикер в top-3 watchlist |
| `DATA_SUFFICIENT` | ≥6 закрытых 5m баров |
| `BENCHMARK_VWAP` | QQQ выше своего VWAP (для не-tech допускается SPY). Fail-closed: нет данных → блок |
| `STRUCTURE_IMPULSE_PULLBACK` | найдена связка импульс→откат→подтверждение, заканчивающаяся последним закрытым баром |
| `IMPULSE_MOVE` | ≥2 5m-свечи (до 1 counter-move при `max_impulse_counter_moves=1`), ход ≥0.8% |
| `IMPULSE_VOLUME` | средний объём импульса ≥1.5x среднего до него |
| `PRICE_TRENDED_VWAP_SIDE` | весь импульс цена держится выше VWAP |
| `PULLBACK_VOLUME` | объём отката ≤0.9x базового |
| `VWAP_TOUCH` | касание/прокол VWAP в полосе ±max(0.10%, ATR%*0.1) — adaptive tolerance (см. `use_adaptive_vwap_tolerance`) |
| `CONFIRM_CLOSE_VWAP` | подтвержд. бар закрылся обратно выше VWAP |
| `STRUCTURE_HL_LH` | минимум отката выше свинга перед импульсом (higher low) |
| `OPENING_RANGE_FILTER` | close подтверждения ≥ середины OR15 (baseline-интерпретация «дополнительного фильтра») |
| `ROOM_TO_LEVEL` | до ближайшего ключевого уровня ≥1.8R. Уровни: prev-day high/low + внешние. Нет уровня впереди → комната неограничена |
| `SIZING` | shares>0, notional≤$5000, риск≤$10 |

SHORT — зеркально (импульс вниз, цена и бенчмарк ниже VWAP, lower high,
вход на пробое low структуры).

## Вход/выход

- Триггер входа: пробой high подтверждающей структуры (+буфер $0.03);
  зона входа `[structure_high, structure_high+buf]`.
- Стоп: за экстремум отката ±буфер ($0.03).
- TP1 = 1R, TP2 = 2R. Расчёт размера: сначала стоп → потом shares
  (`floor($10/risk)` ∩ `floor($5000/entry)`), см. `usstocks/sizing.py`.

## Риск-блокировки (ТЗ §8)

`DAY_STOPPED (оператор)` · `PARTIAL_FILL_ACTIVE` · `PERSONAL_DAILY_STOP (-$20)` ·
`MAX_TRADES_REACHED (2)` · `MAX_CONSECUTIVE_LOSSES (2, cooldown 30 мин)` · `DAILY_PROFIT_LOCK (+$20)` ·
`ACTIVE_POSITION_EXISTS` · `SESSION_CLOSE_GUARD (25 мин до закрытия)`.
Порядок проверки детерминирован; отказы журнализируются.

- `MAX_CONSECUTIVE_LOSSES_COOLDOWN`: после 2 лоссов блок 30 мин, затем снова ALLOW (счётчик сбрасывается только win).
  `last_loss_time` — in-memory only, сбрасывается при рестарте трейдера (намеренно — рестарт = fresh day).
- `PERSONAL_DAILY_STOP`: `unrealized` учитывается только при `active_symbol` (signal-only: без позиции — только realized).

## Параметры стратегии (Phase 1-3)

```yaml
strategy:
  vwap_touch_tolerance_pct: 0.10
  use_adaptive_vwap_tolerance: true   # max(0.10%, ATR%*0.1)
  atr_tolerance_multiplier: 0.1
  max_impulse_counter_moves: 1        # allow 1 counter-move candle
  stop_buffer: 0.03                   # absolute $ (NOT cents)
risk:
  consecutive_losses_cooldown_minutes: 30
scanner:
  max_parallel_workers: 3
  cache_ttl_seconds: 30
```

- VWAP/OR кэшируются на 30s, инвалидация по новому бару (hash) и смене дня.
- Scanner parallel: prefetch benchmarks, ThreadPool 3, gate serial, watchlist order preserved.

## Anchored VWAP (future)

Утилиты `usstocks/indicators.py: anchored_vwap(bars, anchor_idx)`, `find_swing_low/high` — для бэктеста сильных трендов.
Не интегрированы в `evaluate` — включить после валидации +10% win rate на истории.

## Holidays

`config/us_stocks_challenge.yaml` — source of truth. Синхронизация:
```bash
python scripts/update_holidays.py          # generate data/us_market_holidays.json
python scripts/update_holidays.py --check  # CI check (tests/test_session_holidays.py)
```

## ML

`model.enabled: false`, `role: advisory_only`. Модель не может разрешить сделку
или изменить размер; позже — только как quality_score поверх пройденного чеклиста
(после 30–50 журналированных сигналов).
