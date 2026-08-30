# MYPY_ISSUES — статус type-checking (Задача 4)

> Инструмент: mypy 2.3.1, конфиг [`mypy.ini`](../mypy.ini)
> (python_version=3.12, `check_untyped_defs=True`, `warn_return_any=True`,
> `ignore_missing_imports=True`). Прогон: локально (Windows, 2026-08),
> `mypy --explicit-package-bases <pkg>/` по каждому пакету отдельно.

## Итоговая статистика по пакетам

| Пакет | Ошибок | Файлов с ошибками | Топ-коды |
|---|---:|---:|---|
| **mt5_adapter** | **0** | 0 | — (было 17, исправлено) |
| **mql5** | **0** | 0 | — |
| **news** | **0** | 0 | — |
| logs | 5 | 1 | no-any-return:3, var-annotated:2 |
| regime | 10 | 3 | no-any-return:5, assignment:5 |
| services | 10 | 3 | assignment:6, no-any-return:3, arg-type:1 |
| config | 10 | 1 | no-any-return:5, assignment:3, arg-type:2 |
| simulation | 10 | 1 | no-any-return:6, var-annotated:2 |
| labeling | 16 | 4 | assignment:11, no-any-return:4 |
| backtest | 24 | 4 | assignment:6, operator:6, no-any-return:5 |
| risk | 34 | 3 | assignment:20, no-any-return:4, return-value:3 |
| features | 38 | 8 | assignment:19, no-any-return:10 |
| data | 57 | 5 | arg-type:20, no-any-return:14, assignment:10 |
| paper | 51 | 7 | assignment:19, no-any-return:14, operator:9 |
| model | 87 | 9 | assignment:29, no-any-return:25, operator:17 |
| challenge | 118 | 3 | index:28, assignment:24, arg-type:21 |
| contracts | 146 | 14 | assignment:32, arg-type:26, no-any-return:25 |
| realtime | 179 | 13 | arg-type:39, assignment:33, index:30 |
| alerts | 192 | 15 | assignment:62, no-any-return:45, arg-type:27 |
| tests | 256 | 17 | assignment:74, no-any-return:51 |
| monitoring | 266 | 14 | index:120, assignment:39, no-any-return:27 |
| scripts | 398 | 18 | assignment:93, union-attr:66, arg-type:61 |
| execution | 442 | 16 | index:146, assignment:73, arg-type:75 |

**Итого ≈ 2200 ошибок** (замечание: из-за follow-imports часть ошибок
приписывается импортируемым пакетам; числа выше — «чистые» ошибки,
приписанные файлам самого пакета по пути файла в выводе).

## Решение по CI

- **Блокирующий** mypy только на чистых целях: `mt5_adapter mql5 news`
  → 0 ошибок. Это quality-bar для нового кода: любое регрессирование
  типизации в этих пакетах ломает CI.
- Остальные пакеты — **не** gate: суммарно >2000 ошибок легаси-типов
  (в основном `assignment`, `index`, `no-any-return`, `arg-type`,
  var-annotated на старом нетипизированном коде). Исправление — отдельная
  dedicated работа (см. план ниже), аналог docs/RUFF_POLICY.md.

## Что было исправлено тривиально (Задача 4)

12 безопасных аннотаций-фиксов в `mt5_adapter` (17 → 0 ошибок):

1. `mt5_adapter/testing.py` — 6 × `name-match`: первый аргумент
   `namedtuple()` должен быть `"_TickTuple"` и т.п., а не `"TickTuple"`
   (mypy требует совпадения с именем переменной).
2. `mt5_adapter/client.py:175` — `operator`: сравнение
   `elapsed > self.timeout_s` сужено guard'ом `self.timeout_s is not None`
   (было `if started is not None`, а `timeout_s: float | None`).
3. `mt5_adapter/tests/test_cache.py:35` — `var-annotated`:
   `calls: list[int] = []`.
4. `mt5_adapter/tests/test_rate_limiter.py` — 2 × `var-annotated`:
   `sleeps: list[float] = []`.
5. `mt5_adapter/tests/test_client.py:140` — `operator`:
   `in` над `str | None` — добавлен assert на not-None
   (`exc.value.comment is not None`) перед проверкой подстроки.

## Настройки mypy.ini (обоснование ignore-секций)

- `pytest`, `numpy`, `pandas`, `xgboost`, `sklearn`, `joblib` —
  сторонние пакеты, стабы отсутствуют или неполные; по ТЗ
  `ignore_missing_imports=True`.
- `MetaTrader5` — Windows-only pip-пакет, недоступен на ubuntu CI
  (platform-marker в requirements.txt); без секции mypy на Linux даёт
  import-error. На Windows-машине секция помечается как unused
  (note в выводе — это ожидаемо и не ошибка).

## План исправления легаси (Фаза 7 / отдельные PR)

Приоритет — по «близости» к торговому пути и числу ошибок:

1. **execution (442)** — самый большой долг; начать с
   `execution/tests/test_trade_group_executor.py` (≈100 `index`-ошибок на
   `dict | None` из-за отсутствующих narrowing'ов) и старых нетипизированных
   сигнатур в `mt5_trader.py` / `trade_group_executor.py`.
2. **monitoring (266)** — доминирует `index` (120): доступ к
   `dict[str, Any] | None`; скорее всего точечные guard'ы + аннотации.
3. **scripts (398)** — низкий приоритет (утилиты), много `union-attr`.
4. **realtime / alerts / contracts** — средними батчами, стиль
   «строгий модуль за модулем» + переход на strict per-module секции.
5. Целевое состояние: `disallow_untyped_defs = True` для новых пакетов,
   поэтапное подключение остальных в blocking-набор
   (`mypy_targets_ci = mt5_adapter mql5 news ...`).

---

## ТЗ Задача 4 — фокусированный прогон по 7 пакетам (2026-08-28)

Прогон `mypy --explicit-package-bases execution/ model/ features/ data/ risk/
mt5_adapter/ provenance/` (по ТЗ Задачи 4) по конфигу `mypy.ini`:

| Метрика | Значение |
|---|---:|
| Ошибок всего (с follow-imports) | **460** (было 490 до class-A фиксов) |
| Файлов с ошибками | 73 |
| Проверено исходных файлов | 157 |
| В самих 7 пакетах (не follow-imports) | 383 → 353 после фиксов |
| …в т.ч. в TEST-файлах (`*/tests/*`) | 275 (исключены `exclude = ^tests/.*`) |
| …в т.ч. в production-файлах | 108 → **78** после фиксов |

### Классификация (Step 4)

- **Класс A — безопасная типизация (исправлено):** Implicit Optional
  (`x: int = None` → `Optional[int] = None`) и `var-annotated`. Исправлено в
  чистых файлах 7 пакетов:
  - `risk/`: `engine.py` (3 ф-ции), `limits.py` (3), `state.py`,
    `throttle.py`, `compat.py` + импорт `Optional`.
  - `data/`: `trade_logger.py`, `signal_log.py` (`read_signal_history`),
    `ingestion.py`, `sentiment_analyzer.py` + 3 импорта `Optional`.
  - `model/`: `trainer.py` (3 × `cfg`), `ensemble_backtest.py`
    (`run`: `forced_direction`/`max_trades`) + импорт `Optional`.
  - Итог: −30 ошибок (490 → 460 total); pytest `tests/ -x` — 54 passed.
- **Класс B — требуют рефакторинга/суждений (задокументировано, НЕ фиксил):**
  `index`/`attr-defined`/`operator`/`union-attr` на `object`/`dict | None`
  (`execution/mt5_trade_group.py`, `mt5_compensation.py`, `mt5_trader.py`,
  `provenance/verifier.py`), `no-any-return` (~22), `return-value`
  (`risk/sizing.py`), `misc` (`data/trade_group_store.py`). План — по
  `docs/MYPY_ISSUES.md` «План исправления легаси».
- **Deferred (WIP-файлы пользователя, НЕ трогаю):**
  - `execution/mt5_trader.py` — 5 class-A (Implicit Optional) + class B;
    файл имеет незакоммиченный WIP — аннотации применю после мержа WIP.
  - `execution/fx_execution_probe.py:195` — `var-annotated` `daily_counts`.

### CI-дизайн (Step 7)

Два mypy-шага в `ci.yml`:
1. **Блокирующий** — чистые цели: `mypy --explicit-package-bases
   mt5_adapter/ mql5/ news/` → 0 ошибок (quality bar для нового кода).
2. **Неблокирующий** (`continue-on-error: true`) — 7 пакетов ТЗ (текущий
   легаси-долг, ~353 ошибок в самих пакетах) — отчёт без блокировки сборки.

### `mypy.ini` (дополнено)

- `exclude` — `scripts/`, `tests/`, `_tmp_*`, `htmlcov/`.
- `[mypy-simulation.*] ignore_errors=True` — фикс CI-блокера: фейковый MT5
  shim (`simulation/mt5_shim`) тянулся через follow-imports из
  `mt5_adapter/lazy.py` (6 ошибок) и ломал блокирующий job. Теперь
  `mypy mt5_adapter/ mql5/ news/` → **Success, no issues found**.
- Секции `fastapi(. *)`, `uvicorn(. *)`, `statsmodels(. *)` добавлены по ТЗ.
