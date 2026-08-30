# Runbook эксплуатации (ТЗ Часть 11, §11.3)

Ежедневные операции: запуск/останов, health, алерты, бэкапы, миграции,
безопасность, overnight-пайплайн. Формат — команды и правила, без теории.

---

## 1. Запуск / останов

### Основной бот (виртуальный фид + торговый цикл + Telegram-контроль)

```bash
python -m scripts.run_bot
```

* Перед стартом **обязательно** применяет миграции БД
  (`_apply_db_migrations` → `data.migrate.apply_migrations`); ошибка миграции =
  бот не стартует (fail-closed, ТЗ 9.3).
* Env-кнопки: `DRY_RUN=1` (лог ордеров без отправки),
  `SIMULATION_SEED`, `SIMULATION_WARMUP_TICKS` (default 5000).
* Telegram-команды: `/start`, `/help`, `/status`, `/positions`, `/why ASSET`,
  `/metrics`, `/account`, `/pause` (→ dry-run), `/resume`, `/closeall`.

### Автономные сервисы (ТЗ 8.1/8.2/8.8)

```bash
python -m services.ledger_bridge [--once] [--health-port 8791]
python -m services.telegram_bot  [--health-port 8792]
python -m services.news_feed     [--health-port 8793]
```

Каждый сервис поднимает собственный `GET /health`
(`services/base.py::create_health_app`); статус `ok` / `degraded`,
падение одного чека не даёт 500.

### Graceful shutdown

`scripts/run_bot.py::_install_shutdown_handlers`: SIGTERM/SIGINT →
`executor.shutdown()` → exit 0. На Windows SIGTERM ограничен; SIGINT
(KeyboardInterrupt) покрывает консольный случай. Не завершать процесс
принудительно (kill -9): леджер/группы могут остаться в промежуточном состоянии
(восстановление — `execution/reconciliation.py`, см. docs/RECOVERY.md).

### Режимы

* **DeploymentMode** (`config/deployment.py`): `research` / `simulation` /
  `paper` / `live`; маршрутизация реальных ордеров только при
  `order_routing_allowed` (в live — с явным подтверждением).
* **DATA_MODE** (env, default `mock`): режим данных дашборда/пайплайна.
  `run_bot` / `run_simulation` ставят `DATA_MODE=live` (реальный канал через
  виртуальный MT5-shim/терминал). Эндпоинты честно показывают
  `source/mode/as_of`; в не-live режимах реальные рыночные данные
  подменяться не могут (HTTP 503, `reason: real_market_data_required`).

---

## 2. Health / метрики

| Эндпоинт / файл | Что показывает |
|---|---|
| `GET /api/health` (`realtime/app.py` + `monitoring/health.py`) | агрегат проверок: `db` (доступность БД + статус миграций), `executor` (активные группы), `risk` (circuit breaker из risk_state), `feed` (свежесть тиков по символам), `services` (порты health сервисов, без сети) |
| `GET /api/execution-metrics` | агрегаты `monitoring/metrics.py::MetricsCollector` — rejection-причины, латентности стадий; под bearer-гейтом (ТЗ 6.1) |
| `logs/metrics.jsonl` | потоковые метрики; ротация; путь — config `monitoring.metrics.jsonl_path` |
| `GET /health` сервисов | порты 8791 (ledger_bridge), 8792 (telegram_bot), 8793 (news_feed) |

---

## 3. Алерты

`monitoring/alerts.py::AlertManager` — правила с per-rule cooldown:

| Правило | Условие | Cooldown | Серьёзность |
|---|---|---|---|
| `FEED_STALE` | последний тик старше `feed_stale_after_s` (default 30) | 600s | P1 |
| `CIRCUIT_BREAKER` | сработал риск-брейкер | 3600s | P0 |
| `DISK_LOW` | свободное место < `disk_min_free_mb` (default 500) | 1800s | P1 |
| `MT5_DISCONNECT` | `symbol_info_tick` None N раз подряд (default 5) | 600s | P1 |

Включение — config `monitoring.alerts.enabled` (по умолчанию выключено).
Уведомления — Telegram (`alerts/telegram_bot.py`), lazy-импорт.
Админ-команды контроля (`/pause`, `/resume`, `/closeall`) разрешены только
чатам из env `TELEGRAM_ADMIN_IDS` (список через запятую, ТЗ 10.3);
неавторизованные команды логируются и отклоняются.

---

## 4. Бэкап / восстановление

```bash
# Бэкап: консистентная копия главной БД (sqlite3 online-backup API)
# + logs/risk_state.json, с ретенцией (monitoring.backup.keep, default 7)
python -m scripts.backup_db [--db-path PATH] [--backup-dir DIR] [--dry-run]

# Восстановление (ТЗ 6.10): ДЕСТРУКТИВНО, после integrity-check
python -m scripts.backup_db --restore backups/market_data_mt5.sqlite.bak --yes
```

* `--restore` без `--yes` и без TTY-подтверждения → exit 2 (защита от случайного запуска).
* Полная процедура восстановления: **docs/RECOVERY.md**.

---

## 5. Миграции

```bash
# Dry-run: показать план по всем известным БД + registry-check, ничего не менять
python -m scripts.migrate_all --dry-run

# Применить
python -m scripts.migrate_all
```

`scripts/migrate_all.py` (ТЗ 9.11): 1) БД-миграции для главной БД
(config `general.db_path`), trade-log БД (`TRADE_LOG_DB_PATH`) и signal-log БД
(`SIGNAL_LOG_DB_PATH`); 2) registry-check — прогон
`TradeGroupSpec`/`ExecutionIntent` payloads через schema registry, чтобы
неизвестные версии падали ДО старта рантайма.

`scripts/run_bot.py` применяет миграции главной БД автоматически при старте;
ошибка = фатальна. Подробности: docs/MIGRATIONS.md.

---

## 6. Безопасность

* **API auth**: env `API_AUTH_TOKEN` + config `security.api.require_auth`
  (env `API_REQUIRE_AUTH=1/true` перекрывает конфиг). Fail-closed: включённый
  `require_auth` без токена — приложение не стартует
  (`realtime/app.py::validate_api_auth_startup`). По умолчанию require_auth
  выключен — допустимо только для loopback-деплоя.
* **require_auth middleware**: глобальный bearer-гейт поверх `/api/*`
  (кроме `/api/health`), см. `realtime/tests/test_api_auth.py`.
* **Telegram**: мутации только от `TELEGRAM_ADMIN_IDS` (ТЗ 10.3).
* **Аудит репозитория**: `python -m scripts.security_audit [--json]`
  (ТЗ 10.2/10.10) — .gitignore/.env, захардкоженные секреты, права на файлы,
  *.sqlite в git. Exit 1 при находках.
* **Секреты** — только в `.env` (git-ignored): `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_IDS`, `API_AUTH_TOKEN`,
  `MODEL_REGISTRY_ROOT`, `TRADE_LOG_DB_PATH`, `SIGNAL_LOG_DB_PATH`,
  `PROVENANCE_STORE_DB_PATH`, `METRICS_JSONL`, `DATA_MODE`, `MODEL_PATH`.

---

## 7. Overnight-пайплайн

```bash
python -m scripts.overnight
```

Стадии (каждая — обёртка над существующим entry point, изоляция падений):

1. `backfill_fresh_data` — `scripts.backfill_data` (догрузка свежих свечей MT5)
2. `walk_forward_backtest` — `scripts.run_backtest` (health-check моделей)
3. `retrain_models` — `scripts.train_all_assets`
3b. `deploy_guard_backup` — `scripts.deploy_guard --backup` (снапшот прод-моделей)
4. `retrain_with_real_trades` — `scripts.retrain_with_real_trades`
4b. `deploy_guard_check` — walk-forward против бэкапа; регресс → restore + exit 1
4c. `verify_model_fingerprints` — аудит хешей и дегенерации вероятностей
5. `summary_report` — агрегация `logs/backtest_*.csv`
6. `telegram_notify` — сводка в Telegram (если настроен)

**Мониторы (включаются по конфигу):**

* Feature-drift (P2-40): `scripts.monitor_feature_drift` — PSI по фичам
  (train vs live), порог 0.2; config `monitoring.drift`.
* Калибровка (P2-46): `scripts.monitor_calibration` — Brier + ECE (порог 0.1);
  config `monitoring.calibration`.

Env-ручки overnight: `OVERNIGHT_BACKFILL_DAYS` и др. — см. docstring
`scripts/overnight.py`.