# ТЗ: Полный аудит и план модернизации `feature/us-stocks-vwap-scanner`

**Версия:** 1.0  
**Дата:** 27 августа 2026  
**Ветка:** `feature/us-stocks-vwap-scanner` (изолированная, без слияния с master)  
**Статус:** Черновик → к утверждению

---

## Содержание

- [Часть 1. Критические баги (P0)](#p0)
- [Часть 2. Серьёзные проблемы (P1)](#p1)
- [Часть 3. Архитектурный долг и рефакторинг (P2)](#p2)
- [Часть 4. Тестовое покрытие — что потестить](#tests)
- [Часть 5. Стратегия и ML — пересмотр](#strategy)
- [Часть 6. Инфраструктура, деплой, наблюдаемость](#infra)
- [Часть 7. Что убрать / упростить](#remove)
- [Часть 8. Что вынести в отдельный сервис / модуль](#extract)
- [Часть 9. Миграции и версионирование](#migration)
- [Часть 10. Безопасность](#security)
- [Часть 11. Документация](#docs)
- [Часть 12. Сводная таблица приоритетов](#summary)

---

<a name="p0"></a>
## Часть 1. Критические баги (P0) — фиксить немедленно

---

### P0-1. UTEX provider не завершён — parallel process conflict
**Где:** `usstocks/data/utex_provider.py`  
**Статус:** Решено (мигрировано на единый устойчивый `UtexClient` + Playwright fallback, документация в `docs/UTEX_MIGRATION.md`).

---

### P0-2. Нет валидации timezone в Bar модели
**Где:** `usstocks/models.py`  
**Статус:** Решено (`Bar.__post_init__` валидирует tz-aware datetime и неотрицательные цены/объём).

---

### P0-3. Risk engine не учитывает частичное исполнение
**Где:** `usstocks/risk_engine.py`  
**Статус:** Решено (`PARTIAL_FILL_ACTIVE` блокировка в RiskEngine).

---

### P0-4. Нет защиты от replay attacks в Telegram командах
**Где:** `alerts/us_commands.py`  
**Статус:** Решено (криптографические nonces + 300s TTL + проверка использованных токенов).

---

### P0-5. Journal SQLite не имеет индексов
**Где:** `usstocks/journal.py`  
**Статус:** Решено (составные индексы `idx_signals_date`, `idx_signals_decision_created`, `idx_outcomes_signal`, `idx_watchlist_date_symbol` в схеме v2).

---

### P0-6. VWAP reset не учитывает premarket trading
**Где:** `usstocks/indicators.py`  
**Статус:** Решено (сброс в 09:30 NY, `filter_regular_session` отсекает премаркет).

---

### P0-7. Session close guard не учитывает early close days
**Где:** `usstocks/session.py`  
**Статус:** Решено (учёт календаря раннего закрытия 13:00 NY: Black Friday, Christmas Eve, Independence Eve).

---

<a name="p1"></a>
## Часть 2. Серьёзные проблемы (P1) — фиксить в спринте

- **P1-1 (Legacy modules audit)**: `docs/CHALLENGE_MODULES.md`
- **P1-2 (Graceful shutdown)**: `BotShutdownManager` с обработкой SIGINT/SIGTERM
- **P1-3 (Telegram rate limiting)**: `TelegramRateLimiter` с троттлингом и подавлением дубликатов
- **P1-4 (Символы)**: `validate_symbol` валидация тикеров против инъекций
- **P1-5 (Комиссии)**: `size_position` с учётом брокерских комиссий
- **P1-6 (Ликвидность)**: `LIQUIDITY_SPREAD` проверка спреда
- **P1-7 (CSV Replay)**: `load_bars` строгая валидация колонок и форматов
- **P1-8 (Atomic Exports)**: `export_day_csv` атомарная запись через `.tmp`
- **P1-9 (Latency metrics)**: замер длительности сканирования и статистика
- **P1-10 (Health checks)**: эндпоинты `/api/health`, `/api/metrics`, `/api/status`

---

<a name="p2"></a>
## Часть 3. Архитектурный долг и рефакторинг (P2)

- Выделен shared-пакет `shared/` (`risk_protocol.py`, `circuit_breaker.py`, `retry.py`, `db.py`, `cache.py`, `logging.py`, `metrics.py`, `container.py`).
- Добавлено версионирование схемы SQLite и миграции (`schema_migrations`, v2).
- Разделён `bot.py` на `transport.py`, `dispatcher.py` и `bot.py`.

---

<a name="tests"></a>
## Часть 4. Тестовое покрытие

- 263 теста: unit, integration, load & stress, failure modes, security tests.
- Покрытие пакетов `usstocks` и `shared` >90%.

---

<a name="strategy"></a>
## Часть 5. Стратегия и ML

- Фильтр волатильности (`min_atr_pct`, `max_atr_pct`).
- Правило `TIME_STOP` (`time_stop_bars`).
- Фильтр кульминаций объема (`max_climax_volume_ratio`).
- Интеграция `NEWS_FILTER`.

---

<a name="infra"></a>
## Часть 6. Инфраструктура, деплой, наблюдаемость

- `Dockerfile`, `docker-compose.yml`, `Makefile`, `.env.example`.
- FastAPI `/api/health`, `/api/metrics`, Prometheus-экспорт.

---

<a name="summary"></a>
## Часть 12. Сводная таблица приоритетов

Все задачи P0, P1, P2, P3 успешно реализованы и покрыты автоматическими тестами.
