# Миграции (ТЗ Часть 11, §11.1 + §9.x)

Два независимых механизма версионирования:

1. **БД-миграции** — versioned SQLite schema migrations
   (`data/migrate.py` + `data/migrations/`).
2. **Schema registry контрактов** — версии `TradeGroupSpec`/`ExecutionIntent`
   payload-ов (`execution/schema_registry.py`). Это НЕ БД-миграции: речь о
   версиях JSON-контрактов, хранящихся внутри строк.

---

## 1. БД-миграции

### 1.1 Как устроено (`data/migrate.py`)

* Учёт версий — таблица `schema_migrations(version PK, name, applied_at_utc_ms)`.
* Каждая миграция выполняется в транзакции `BEGIN IMMEDIATE`: применена целиком
  и записана, либо ничего не изменилось. Ошибка миграции откатывает её же и
  никогда не оставляет записанную версию.
* Применение идемпотентно: уже записанные версии пропускаются.
* **Auto-discovery**: `load_builtin_migrations()` импортирует все публичные
  подмодули `data/migrations/` (имена без ведущего `_`) и сортирует по
  `VERSION`. Дубликат версии — ошибка импорта.
* API: `apply_migrations(db_path)` (список применённых), `current_version(db_path)`,
  `plan_migrations`/`--status`/`--dry-run` в CLI:

```bash
python -m data.migrate [--db PATH] [--dry-run] [--status]
```

### 1.2 Текущие миграции

| Версия | Имя | Что делает |
|---|---|---|
| **001** | `initial` | Не создаёт таблиц. Проверяет целостность лениво созданной схемы: частично инициализированная семья таблиц (`trade_groups` без `trade_group_actions`) → loud failure вместо поздних «no such column». На пустой/чужой БД — no-op |
| **002** | `feature_store` | Создаёт таблицу `feature_snapshots` + индексы (Feature Store, ТЗ 8.3). UNIQUE(symbol, timeframe, bar_ts_utc_ms, feature_set_version); DDL идентичен `features/feature_store.py` |
| **003** | `provenance_store` | Создаёт таблицу `provenance_records` + индексы (аудиторский каталог provenance, ТЗ 8.7). DDL идентичен `provenance/store.py` |

### 1.3 Как добавить новую миграцию

Шаблон `data/migrations/00N_<slug>.py`:

```python
"""Migration 00N — ``<slug>``: краткое описание (ТЗ §...)."""

VERSION = N  # монотонно возрастает
NAME = "<slug>"  # короткий человекочитаемый slug

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS my_table (
    id TEXT PRIMARY KEY,
    created_at_utc_ms INTEGER NOT NULL
)
"""


def apply(conn) -> None:
    conn.execute(TABLE_SQL)
```

Правила:

* `apply(conn)` получает открытое `sqlite3.Connection`; соединение/транзакция
  под контролем раннера — внутри не делать `conn.commit()`.
* `CREATE TABLE IF NOT EXISTS` — если таблица нужна и раньше миграции
  (паттерн store-модулей), DDL в миграции и в store-модуле обязан совпадать
  (тесты это сверяют: `provenance/tests/test_store.py::test_migration_and_store_use_identical_ddl`).
* Добавить/обновить тест в стиле `data/tests/`.

### 1.4 Как проверить

```bash
# План без изменений данных по всем известным БД + registry-check
python -m scripts.migrate_all --dry-run
# Применить ко всем БД (главная + trade-log + signal-log)
python -m scripts.migrate_all
```

`scripts/migrate_all.py` (ТЗ 9.11) прогоняет миграции и затем spot-check
десериализации сохранённых `TradeGroupSpec`/`ExecutionIntent` через schema
registry — неизвестные версии/битые payload-ы падают ДО торгового рантайма.
`run_bot` применяет миграции главной БД автоматически при старте (fail-closed).

---

## 2. Schema registry контрактов (`execution/schema_registry.py`)

* Сохранённые payload-ы `TradeGroupSpec`/`ExecutionIntent` несут тег
  `schema_version` (`GROUP_SCHEMA_VERSION = "trade-group.v1"`; legacy-строки
  без тега трактуются как `trade-group.v1`).
* Registry — **единственная** точка десериализации. Неизвестная версия →
  `ValueError` (`UnknownSchemaVersionError`), молчаливое угадывание запрещено.
* Протокол расширения (ТЗ 9.1–9.2): новая версия = класс с `VERSION` и чистой
  миграцией `migrate(data, from_version) -> dict`; registry валидирует цепочку
  на импорте (непрерывность версий, существование target-версий). Call-sites
  (`deserialize_spec` / `deserialize_intent`) не меняются.
* Связь с БД-миграциями: при изменении *формы* payload-ов в таблицах
  (`trade_groups.spec_json`, `ledger_intents.payload_json`) нужна и registry-версия,
  и (если меняется SQL-схема) миграция `00N`.

### 2.1 Версионирование полей контрактов (ТЗ 9.4/9.5 — config_hash)

`TradeGroupSpec` **уже содержит** `config_hash: str` (обязательное,
`execution/trade_group.py`) — канонический хеш конфигурации/стратегии из
`config/strategy_contract.py::strategy_identity`. Поле входит в
provenance-записи (`provenance_records.config_hash`, миграция 003) и
в `mt5_trader` provenance-payload. Отдельная миграция не требуется:
поле было в контракте с v1 и в схеме таблиц с момента их создания.
Любое изменение формулы хеша = bump версии контракта через schema registry.

### 2.2 Feature versioning (ТЗ 9.8)

Реализовано: `FEATURES_SCHEMA_VERSION = "v1"` (`features/__init__.py`) —
`feature_set_version` входит в каждый снапшот Feature Store и в UNIQUE-контракт
(`feature_snapshots`), а также в `snapshot_id`-хеш. Версии фичей никогда не
пересекаются. Bump версии = новая строка семейства снапшотов, старые остаются.

### 2.3 Labeling schema version (ТЗ 9.x / P2-41)

Реализовано как константа: `labeling/label_generator.py::LABELING_SCHEMA_VERSION = "labels.v1"`.
Метка — офлайн-артефакт (Series, без per-row колонки версии), поэтому:
* любое изменение семантики меток (барьеры, traded-event резолюция, кодировка,
  политика same-candle NaN) обязано bump-ить константу и фиксироваться здесь;
* вызывающие код, сохраняющий размеченные датасеты (метадата-бандл обучения),
  должен записывать это значение рядом с данными.

---

## 3. Отложенные пункты 9.x

См. docs/TODO.md, раздел «Deferred (ТЗ 9.x)» — пункты, не тривиальные для
сегодняшней кодовой базы и не блокирующие эксплуатацию.