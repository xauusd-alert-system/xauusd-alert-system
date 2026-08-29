# План выполнения ТЗ xauusd-alert-system

**Источник:** `TZ_xauusd_alert_system.md` (~210 задач, Части 1-12)
**Принцип:** безопасность > надёжность > функциональность > скорость.
Каждый шаг = отдельный коммит по формату `<тип>(<область>): <описание>` + тест.

---

## Статус окружения (зафиксировано при планировании)

- Пакетная структура: `execution/`, `data/`, `model/`, `features/`, `alerts/`, `backtest/`, `scripts/` — все с тестами в `<pkg>/tests/`.
- Схема версионирования: `GROUP_SCHEMA_VERSION` уже существует в `execution/trade_group.py` и `execution/execution_intent.py` — реестра и миграций нет.
- Миграций БД нет: все таблицы создаются через `CREATE TABLE IF NOT EXISTS` в `data/*.py`.
- Корень замусорен артефактами: `equity_curve.html`, `session_index.txt`, `check_btcusd_durations.py`, `dump_*.py`, `truncate_db.py`, `fix_ingestion.py`.
- `.env` присутствует в рабочей копии — убедиться, что он в `.gitignore`.

---

## Фаза 1. Фундамент

### Шаг 0. Проверка окружения
- [ ] `pytest -q` — 100% pass до начала работы; если падает — чинить первым делом
- [ ] `git status` чистый; `.env` в `.gitignore`; коммитим в ветку `refactor/master-plan`

### Шаг 1. P0-фиксы (Часть 1) — порядок от простого к сложному
| Задача | Где | Что делать | Тесты |
|---|---|---|---|
| P0-7 отклонение actual_fill | `execution/trade_group.py::with_actual_fill` | проверка ±5% (порог из конфига) | `test_actual_fill_deviation_rejected` |
| P0-3 estimated_loss в hash | `execution/trade_group.py::geometry_hash` | убрать исключение из risk dump | `test_geometry_hash_includes_risk` |
| P0-1 TTL по таймфрейму | `execution/trade_geometry.py`, config.yaml | карта `signal_ttl_ms`, дефолт 2ч | `test_signal_ttl_by_timeframe` |
| P0-2 ATR sanity | `calculate_geometry` | 0.05%…3% от цены, пороги в конфиге | 4 теста из ТЗ |
| P0-4 VWAP fill | `mt5_trade_group.py::_open_group` | volume-weighted avg + warning > 0.1% | `test_actual_fill_vwap` |
| P0-6 Decimal для объёмов | `_floor_to_step`, `allocate_leg_volumes` | `Decimal(str(x))` ROUND_DOWN | `test_floor_to_step_dust/large`, `test_allocate_small_volume_dust` |
| P0-5 CB без свопов | `risk_manager.py::can_trade` | balance-based дневной PnL, конфиг `exclude_swaps` | `test_circuit_breaker_ignores_swaps` |

После каждого фикса: тест + коммит. В конце фазы: полный регрессионный прогон.

### Шаг 2. Миграции БД (Часть 9, п.9.3)
- [ ] Таблица `schema_migrations(version INTEGER PRIMARY KEY, applied_at_utc_ms)`
- [ ] Папка `data/migrations/__init__.py` + `001_initial.py` (текущая схема, идемпотентно)
- [ ] `data/migrate.py`: apply/dry-run/status, `BEGIN IMMEDIATE`, блокировка гонок
- [ ] Вызов миграции при старте `scripts/run_bot.py`
- [ ] Тесты: `data/tests/test_migrate.py`

### Шаг 3. Реестр схем (Часть 9, п.9.1–9.2)
- [ ] `execution/schema_registry.py`: SCHEMA_VERSIONS, `deserialize_spec()`, цепочка `migrate()`
- [ ] Переключить `data/trade_group_store.py` на `deserialize_spec`
- [ ] Аналогично для ExecutionIntent (9.2)
- [ ] Тест: v1 → v2 round-trip (`test_spec_v1_migrates_to_v2`)

### Шаг 4. Единый скрипт миграций (9.11)
- [ ] `scripts/migrate_all.py`: DB-миграции + schema registry check + dry-run
- [ ] Интеграция в запуск бота (м/front-runner перед DATA_MODE)

---

## Фаза 2. Адаптеры и модули

### Шаг 5. MT5 Adapter Layer (8.6)
- [ ] `mt5_adapter/client.py` (обёртка всех mt5.*), `rate_limiter.py` (P2-7), `cache.py` (symbol_info/tick кэш с TTL), `errors.py` (типы ошибок), `types.py`
- [ ] Заменить ВСЕ прямые вызовы `mt5.*` вне пакета (grep-проверка + тест-гвард `test_no_direct_mt5_calls`)
- [ ] Mock-адаптер для юнит-тестов

### Шаг 6. Risk Engine (8.5)
- [ ] `risk/engine.py`, `limits.py`, `sizing.py`, `throttle.py`, `state.py`
- [ ] Перенести: `risk_manager.py` → engine/limits (+P0-5), `risk_sizer.py` → sizing/throttle (капы P1-4 обязательными), `trade_throttle.py` → throttle (только rate-based, P2-10)
- [ ] Единая точка `can_open()`; старые файлы удалить после перенаправления импортов
- [ ] P1-7 сюда же: разделение circuit breaker / drawdown throttle, HWM в state.json

### Шаг 7. Model Registry (8.4)
- [ ] `model/registry.py`: register/activate/rollback, fingerprint
- [ ] Интеграция в `train_all_assets` и `deploy_guard`

---

## Фаза 3. Сервисы

### Шаг 8. Feature Store (8.3)
- [ ] `features/feature_store.py`: compute_and_store/get_latest, feature_snapshot_id
- [ ] Интеграция в realtime пайплайн

### Шаг 9. Provenance (8.7)
- [ ] `provenance/spec.py, store.py, verifier.py, api.py`
- [ ] TTL снапшотов (P2-51), bulk audit endpoint (P2-3)
- [ ] Обновить вызовы `execution/provenance.py` и `data/provenance.py`

### Шаг 10. Вынос сервисов (8.1, 8.2, 8.8)
- [ ] `services/ledger_bridge/` (отдельный процесс + health check)
- [ ] `services/telegram_bot/` (+ auth по user_id, P2-54)
- [ ] `services/news_feed/` (фоновый воркер)

---

## Фаза 4. Безопасность и инфраструктура

### Шаг 11. Безопасность (Часть 10)
- [ ] 10.1 Bearer-token аутентификация API + rate limiting
- [ ] 10.2 Секреты только через env, права 600, pip-audit, `.env.example` полный (P2-25)
- [ ] 10.3 Telegram admin whitelist
- [ ] 10.4 Observer protocol_version (P2-50) + подпись сообщений
- [ ] 10.5–10.9 SQL-параметризация, без shell=True, CORS whitelist (P2-34 → origin list)

### Шаг 12. Мониторинг (Часть 6)
- [ ] 6.1 метрики исполнения (`/api/execution-metrics`, metrics.jsonl)
- [ ] 6.2 alert manager: rules + cooldowns
- [ ] 6.3 `/api/health` (с timing p95, P2-12)
- [ ] 6.4 graceful shutdown (P2-6)
- [ ] 6.5 бэкапы SQLite по расписанию
- [ ] 6.6 структурированное JSON-логирование + ротация (P2-33)

### Шаг 13. Инфраструктура
- [ ] 6.7 Dockerfile + docker-compose.yml + Makefile
- [ ] 6.8 CI (.github/workflows): pytest + ruff + mypy
- [ ] 6.9 проверки секретов в CI (gitleaks/pip-audit)
- [ ] 6.10 DR runbook: восстановление из бэкапа

---

## Фаза 5. Упрощение и очистка

- [ ] 7.1 удалить неиспользуемые модули (`sentiment_analyzer`? `neural_trainer` P2-36, `portfolio_allocator` P2-11 — решение: интегрировать или перенести в backtest/)
- [ ] 7.2 убрать легаси-форматирование в `alerts/formatter.py` (P2-21)
- [ ] 7.3 корень: переместить утилиты в `scripts/research/`, артефакты → `artifacts/` + .gitignore
- [ ] 7.4 дублирующую логику bifurcation/prop вынести в `common/` (P2-14)
- [ ] 7.6 типизировать провенанс; 7.7 упростить CostSnapshot (+валидация P1-8); 7.9 Pydantic-валидация config.yaml (P2-59)
- [ ] Retention данных (P2-18), индексы ledger (P2-32), truncate_db --dry-run (P2-30)

---

## Фаза 6. Стратегия и ML

- [ ] 5.1 адаптивный triple-barrier
- [ ] 5.2 hard reject в ensemble (P2-47): `ensemble.reject_threshold`
- [ ] 5.3 мониторинг дрейфа фичей (PSI, P2-40) в overnight
- [ ] 5.4 мониторинг калибровки (Brier/ECE, P2-46)
- [ ] Остальные пункты части 5 по приоритетам

---

## Фаза 7. Тесты, документация, оставшиеся миграции

- [ ] E2E тест «сигнал → ордер → TP1» на PaperDriver (P2-20)
- [ ] Интеграционные: конкурентный доступ к БД (P2-49), restart recovery, external close (P2-5)
- [ ] Покрытие ≥ 70%
- [ ] Документация: docs/ARCHITECTURE.md, docs/OPERATIONS.md, docs/MIGRATIONS.md, README обновить
- [ ] Миграции 9.4–9.15: labeling_schema_version, config_hash, mql5 protocol version

---

## Фаза 8. Архитектурный долг

- [ ] P2-1: разбить `mt5_trade_group.py` на mt5_be_flow / mt5_compensation / mt5_netting_close / telegram_formatter (каждый ≤200 строк, основной ≤400)
- [ ] P2-8: DriverResult единый интерфейс драйверов
- [ ] P2-9: MT5HealthChecker (FEED_STALE)
- [ ] P2-22: overnight checkpoints; P2-23: pip-compile lock; P2-24: pyproject пакет
- [ ] Остальные P2/P3 из частей 3 и 5 по мере доступности

---

## Критерии завершения (по ТЗ)

```
pytest -q                        → 100% pass
ruff check . && mypy execution/ model/ features/ data/  → 0 ошибок
pytest --cov=.                   → ≥ 70%
python -m scripts.migrate --dry-run → No pending migrations
pip-audit                        → 0 критических
DATA_MODE=paper python -m scripts.run_bot → /api/health = 200
```

## Правила работы агентов

1. Каждый коммит зелёный по тестам. Каждый фикс/фича = тест.
2. Ничего не удалять без замены и тестов; `.env` и секреты не коммитить.
3. При конфликте ТЗ ↔ код: сначала тест текущего поведения, потом изменение.
4. Невыполнимое → TODO.md + issue, не ломать работающее.
5. Порядок фаз строго линейный; внутри фазы — по возрастанию риска.
