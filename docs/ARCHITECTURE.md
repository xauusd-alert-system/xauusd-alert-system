# Архитектура системы (ТЗ Часть 11, §11.2)

Проект: `xauusd-alert-system` (XAUUSD и мульти-ассет alert/trading system).
Ветка документации: `refactor/master-plan`. Язык проекта — Python 3.11+, основной движок хранения — SQLite.

---

## 1. Обзор системы (пайплайн)

```
MT5 / virtual feed (data/, mt5_adapter/)
        │  OHLCV, тики
        ▼
features/  ── Feature Store (снапшоты, FEATURES_SCHEMA_VERSION)
        │  вектор фичей
        ▼
model/  ── тренер, калибровка, ансамбль (ML + rules), Model Registry
        │  вероятность / сигнал
        ▼
execution/  ── trade_geometry → trade_group (TradeGroupSpec) → executor (MT5)
        │  события исполнения, provenance
        ▼
риск-гейты (risk/engine.py) применяются ДО выставления группы
        ▼
леджер: ledger_intents / ledger_events / executed_trades (+ provenance_records)
```

Каждый шаг пайплайна версионирован: схема контрактов исполнения — через
`execution/schema_registry.py` (см. docs/MIGRATIONS.md), фичи — через
`FEATURES_SCHEMA_VERSION` (`features/__init__.py`), модели — через Model
Registry (`model/registry.py`), конфигурация — `config_hash` в
`TradeGroupSpec` (хеш канонического конфига/стратегии, `config/strategy_contract.py`).

---

## 2. Карта пакетов

| Пакет | Ответственность | Ключевые модули | Кто использует |
|---|---|---|---|
| **data/** | Инжест MT5-данных, SQLite-хранилища (свечи, сигналы, группы, леджеры), миграции БД | `storage.py`, `signal_log.py`, `trade_group_store.py`, `intent_ledger.py`, `migrate.py`, `migrations/001-003` | Все остальные слои; `scripts/migrate_all.py`, `scripts/run_bot.py` |
| **features/** | Вычисление фичей без look-ahead: индикаторы, order flow, анатомия свечей, структура, MTF confluence, bifurcation | `indicators.py`, `order_flow.py`, `structure.py`, `feature_store.py` (`FEATURES_SCHEMA_VERSION = "v1"`) | `realtime/pipeline.py`, `model/trainer.py`, `backtest/` |
| **model/** | Обучение, калибровка (Brier/ECE), уникальность семплов, ансамбль ML+rules, реестр моделей | `trainer.py`, `calibration.py`, `ensemble.py`, `registry.py` (Model Registry: index.jsonl + active.json) | `realtime/pipeline.py`, `scripts/train_all_assets.py`, overnight-пайплайн |
| **execution/** | Геометрия сделки → группы → исполнение MT5, schema-registry контрактов, провенанс-хуки, reconciliation | `trade_geometry.py`, `trade_group.py` (`GROUP_SCHEMA_VERSION`), `trade_group_executor.py`, `schema_registry.py`, `reconciliation.py` | `scripts/run_bot.py`, `provenance/`, `risk/` (compat) |
| **risk/** | Единый риск-движок: дневные лимиты, circuit breaker, sizing (обязательные капы P1-4), rate-throttle, персистентный HWM | `engine.py` (`RiskEngine.can_open`), `limits.py`, `sizing.py`, `throttle.py`, `state.py` | `execution/trade_group_executor.py`, `mt5_trader.py` |
| **mt5_adapter/** | Единственная точка доступа к терминалу MT5: rate-limit, кэш, lazy-инициализация, типизация | `client.py`, `cache.py`, `rate_limiter.py`, `lazy.py`, `testing.py` | `data/`, `execution/`, `realtime/` (запрет прямых вызовов mt5 тестируется) |
| **provenance/** | Аудиторский каталог provenance торговых групп (ТЗ 8.7): полнота, хеш, TTL | `spec.py` (`ProvenanceRecordV2`, `provenance.v2`), `store.py`, `verifier.py`, `api.py` | `execution/trade_group_executor.py` (запись), `realtime/app.py` (API), `scripts/audit_provenance.py` |
| **monitoring/** | Health-чеки, метрики пайплайна, правила алертов, диск | `health.py` (db/executor/risk/feed checks), `metrics.py` (`logs/metrics.jsonl`), `alerts.py` (AlertManager + правила), `disk.py` | `realtime/app.py` (`/api/health`), `scripts/run_bot.py`, services |
| **services/** | Автономные сервисы с собственными health-эндпоинтами (ТЗ 8.1/8.2/8.8) | `ledger_bridge/`, `telegram_bot/`, `news_feed/`, `base.py` | ops-запуск `python -m services.*` |
| **realtime/** | FastAPI-дашборд и realtime-пайплайн сигналов с честными disclosures (source/mode/as-of) | `app.py` (`/api/health`, `/api/execution-metrics`, auth), `pipeline.py`, `book_feed.py` | UI, ops-мониторинг |
| **alerts/** | Telegram-рассылка сигналов, control-бот, admin-whitelist | `formatter.py`, `control_bot.py` (`TELEGRAM_ADMIN_IDS`), `telegram_bot.py` | `scripts/run_bot.py`, `monitoring/alerts.py` |
| **scripts/** | Точки входа ops/CI: run_bot, migrate_all, backup_db, security_audit, overnight, мониторы drift/калибровки | `run_bot.py`, `migrate_all.py`, `backup_db.py`, `overnight.py`, `monitor_*.py` | оператор, cron/scheduler |
| **config/** | Единый источник конфигурации + pydantic-валидация (ТЗ 7.9), режимы деплоя | `loader.py`, `schema.py`, `deployment.py` (`DeploymentMode`), `strategy_contract.py` (`config_hash`) | все пакеты |
| **labeling/** | Офлайн-разметка: triple-barrier / traded-event (без look-ahead) | `label_generator.py` (`LABEL_EVENTS`, `TRADED_ENCODINGS`) | `model/trainer.py`, `scripts/` |

Вспомогательные: `contracts/` (pydantic-контракты сигналов/исполнения),
`regime/` (классификация режимов), `news/` (календарь новостей),
`paper/` (paper-аккумулятор), `simulation/` (MT5-shim для тестов),
`backtest/` (walk-forward, Monte Carlo), `mql5/` (EA SignalDeskObserver).

---

## 3. Ключевые потоки

### 3.1 Сигнал → группа → исполнение

1. `realtime/pipeline.py::_build_features` строит вектор фичей и снапшот в
   Feature Store (`feature_set_version = FEATURES_SCHEMA_VERSION`).
2. `model/ensemble.py` решает ML + rules → вероятности, EV-gate, min confidence.
3. `execution/trade_geometry.py` строит геометрию сделки (TP/SL-уровни, лоты)
   из сигнала; `config_hash` / `model_hash` / `strategy_version` берутся из
   `config/strategy_contract.py::strategy_identity`.
4. `execution/trade_group.py` создаёт `TradeGroupSpec` (`schema_version="trade-group.v1"`).
5. `risk/engine.py::RiskEngine.can_open()` — единая точка всех гейтов
   (лимиты, circuit breaker, throttle, кластеры). Блок → группа не открывается.
6. `execution/trade_group_executor.py` / `mt5_trade_group.py` исполняет группу
   через `mt5_adapter`, пишет действия в `trade_group_actions` и леджер.

### 3.2 Провенанс

* При создании группы executor вызывает `provenance/store.py::ProvenanceStore.record`
  (config `provenance.store.enabled`) — запись `ProvenanceRecordV2`
  (`provenance.v2`) в таблицу `provenance_records` (миграция 003).
* Fail-open: ошибка store никогда не ломает исполнение
  (см. `provenance/tests/test_integration.py`).
* Проверка полноты/хеша/TTL — `provenance/verifier.py::verify_record`
  (P2-3 bulk-аудит: `scripts/audit_provenance.py`, API в `provenance/api.py`).

### 3.3 Риск-гейты

`risk/engine.py` агрегирует: `limits.py` (дневной убыток + circuit breaker,
P0-5 исключение свопов), `sizing.py` (P1-4 обязательные капы, throttle от
персистентного HWM), `throttle.py` (только rate-limit, P2-10 — никаких
дневных лимитов). Состояние — `risk/state.py` → `logs/risk_state.json`
(atomic save, HWM-ratchet P1-7). Старые API — `risk/compat.py` (shims).

---

## 4. Точки расширения (версионирование)

| Механизм | Где | Как расширяется |
|---|---|---|
| **Schema registry контрактов** | `execution/schema_registry.py` | Новая версия `TradeGroupSpec`/`ExecutionIntent` = новый класс с `VERSION`/`MIGRATE`-функцией в цепочку; call-sites не меняются. Неизвестная версия → `ValueError` (fail-closed). Подробности: docs/MIGRATIONS.md |
| **Миграции БД** | `data/migrations/` + `data/migrate.py` | Новый файл `data/migrations/00N_*.py` с `VERSION`/`NAME`/`apply(conn)`; авто-discovery, транзакция `BEGIN IMMEDIATE`. Подробности: docs/MIGRATIONS.md |
| **Feature Store** | `features/__init__.py::FEATURES_SCHEMA_VERSION`, `features/feature_store.py` | Новая версия фичей = bump `FEATURES_SCHEMA_VERSION`; строки разных версий не пересекаются (UNIQUE включает версию) |
| **Model Registry** | `model/registry.py` | Регистрация моделей с sha256 + fingerprint; атомарная активация через `active.json`; rollback активацией предыдущей записи |
| **Provenance schema** | `provenance/spec.py::PROVENANCE_V2_SCHEMA_VERSION` | Новая версия записи `provenance.vN` + адаптеры; `record_hash` канонический |
| **Labeling schema** | `labeling/label_generator.py::LABELING_SCHEMA_VERSION` | Константа версии формата меток (см. MIGRATIONS, §Labeling) |
| **Конфигурация** | `config/schema.py` | pydantic strict-модели: неизвестные ключи ловятся (warn/strict через `CONFIG_VALIDATE_MODE`); `config_hash` связывает конфиг со сделками |

---

## 5. Связанные документы

- docs/MIGRATIONS.md — БД-миграции и schema registry
- docs/OPERATIONS.md — runbook эксплуатации
- docs/RECOVERY.md — восстановление после сбоя
- docs/TRADE_GROUP_SPEC.md — спецификация торговых групп
- docs/STRATEGY_SPEC.md — стратегия