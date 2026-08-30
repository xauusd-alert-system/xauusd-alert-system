# Runbook: восстановление после сбоя (ТЗ 6.10)

Процедура восстановления работы системы после сбоя: повреждение БД, потеря
`risk_state.json`, сбой после обновления, аварийное завершение процесса.

Инструменты: `scripts/backup_db.py` (backup + `--restore`),
`scripts/migrate_all.py`, `scripts/verify_model_fingerprints.py` (model
registry verify). Артефакты: `backups/*.bak` (git-ignored, локальные),
`logs/risk_state.json(.bak)`.

## 0. Определение масштаба

- Процесс упал, но данные целы → просто перезапуск (шаг 6), восстановление не нужно.
- БД не открывается / `database disk image is malformed` / потеря таблиц → восстановление БД (шаг 2).
- Circuit breaker сработал / риск-состояние потеряно → восстановление `risk_state.json` (шаг 3).
- Модели не загружаются / fingerprint mismatch → проверка моделей (шаг 4).

## 1. Остановить процессы

Все процессы должны быть остановлены ДО восстановления, чтобы не было
параллельной записи в БД:

```powershell
# Windows: остановить bot/trader/dashboard (deploy/paper_forward, watchdog)
python -m scripts.process_manager stop   # если используется
# либо завершить процессы python (trader, uvicorn, watchdog) вручную / из Task Manager

# Docker-деплой
docker compose down
```

Убедиться, что нет активных writer'ов: `*.sqlite-wal` / `*.sqlite-shm` рядом с
БД должны перестать расти (после остановки процессов — исчезнуть или остаться
статичными).

## 2. Восстановить БД из backups/

Бэкапы создаёт `python -m scripts.backup_db` (online-backup API, consistent
снимок) в `backups/<имя_бд>.bak`. Проверить свежесть:

```powershell
Get-ChildItem backups | Sort-Object LastWriteTime -Descending | Select-Object -First 5
```

Восстановление (скрипт сам делает `PRAGMA integrity_check` бэкапа, сохраняет
pre-restore копию текущей БД в `<db>.pre_restore.bak` и удаляет stale WAL/SHM
sidecars):

```powershell
# Безопасный вариант (неинтерактивно, для runbook): подтвердить флагом --yes
python -m scripts.backup_db --restore backups/market_data_mt5.sqlite.bak --yes

# Интерактивно (TTY): попросит ввести RESTORE
python -m scripts.backup_db --restore backups/market_data_mt5.sqlite.bak
```

Отказ ожидаем (exit 2) если нет ни `--yes`, ни TTY — так stray-флаг в скрипте
не может молча затереть БД. Отказ (exit 1) если бэкап не прошёл integrity check.

Ручная проверка целостности (если восстанавливаете копированием без скрипта):

```powershell
python -c "import sqlite3; print(sqlite3.connect(r'backups\market_data_mt5.sqlite.bak').execute('PRAGMA integrity_check').fetchone())"
# ожидается: ('ok',)
```

Rollback неудачного restore: верните `<db>.pre_restore.bak` на место.

## 3. Восстановить risk_state.json

`scripts/backup_db.py` бэкапит `logs/risk_state.json` →
`backups/risk_state.json.bak`. `--restore` восстанавливает его автоматически
(если `.bak` существует). Вручную:

```powershell
Copy-Item backups\risk_state.json.bak logs\risk_state.json
```

После восстановления проверить, что circuit breaker не в трипнутом состоянии
(или осознанно сбросить его, если трип был корректной реакцией на реальные
убытки — не сбрасывайте автоматически!).

## 4. Проверить модели (model registry verify)

```powershell
python -m scripts.verify_model_fingerprints
```

Exit 0 — все модели реестра на месте и fingerprints совпадают. Если
fingerprint mismatch: модель пересобрана/повреждена — восстановите артефакты
`models/` из внешнего хранилища (модели НЕ коммитятся и НЕ бэкапятся
`backup_db.py`) либо переобучите (`python -m scripts.retrain_models`) с
последующей записью в реестр.

## 5. Проверить миграции (dry-run)

```powershell
python -m scripts.migrate_all --dry-run
```

Dry-run показывает pending-миграции, ничего не применяя. Если восстановленная
БД отстаёт по схеме — применить реально:

```powershell
python -m scripts.migrate_all
```

## 6. Старт в paper-режиме

Первый старт после сбоя — всегда в paper/mock-режиме, не live:

```powershell
# DATA_MODE=paper|mock читается realtime-пайплайном и планировщиком
$env:DATA_MODE = "paper"
python -m scripts.run_bot
```

Docker: `DATA_MODE` берётся из `.env` (compose `env_file: .env`) — проверьте,
что там не `live`, перед `docker compose up`.

Убедиться, что симулятор/пайплайн поднялись, ордера идут в paper ledger, а не
в брокера.

## 7. Health-проверки

- Dashboard: `GET /api/health` (enriched, компоненты) — `http://127.0.0.1:8000/api/health`;
  публичный liveness `GET /health` без токена.
- Сервисы (если запущены): `GET /health` — ledger_bridge `:8791`,
  telegram_bot `:8792`, news_feed `:8793`.
- Docker: `docker compose ps` — все сервисы `healthy` (healthcheck'и в
  `docker-compose.yml`: dashboard опрашивает `/api/health`, сервисы — `/health`,
  bot — process-liveness по `/proc/1/cmdline`).

Проверить в логах (`logs/`): отсутствие ошибок БД, восстановление риск-состояния,
загрузку моделей. Только после устойчивого green по всем проверкам имеет смысл
переключать `DATA_MODE` на боевой режим (и это отдельное осознанное решение).

## Чеклист (кратко)

1. [ ] Процессы остановлены (bot, dashboard, services, watchdog)
2. [ ] БД восстановлена из `backups/*.bak` (`--restore --yes`), integrity ok
3. [ ] `logs/risk_state.json` восстановлен из `.bak`, circuit breaker проверен
4. [ ] `python -m scripts.verify_model_fingerprints` → exit 0
5. [ ] `python -m scripts.migrate_all --dry-run` → нет неожиданных pending
6. [ ] Старт в `DATA_MODE=paper`
7. [ ] `/api/health` + `/health` сервисов → ok, логи чистые
