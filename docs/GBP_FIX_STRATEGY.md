# GBPUSD Fix Strategy — FX v4 «Развернуть подход» (трендовый)

**Дата:** 2026-08-06  
**Цель:** Сделать GBPUSD прибыльным на честной 2022–26 выборке (24 фолда H1).  
**Принцип:** EUR — реверсивный, GBP — трендовый. FX v3 (ранний БУ) режет восстановления GBP.

## 1. Диагностика (Этап 1)

Скрипт: `python -m scripts.diag_gbp_profile`

Вывод `logs/diag_gbp_profile.csv` + stdout:

- Распределение `exit_reason` (stop / breakeven / tp3_runner / timeout) + средний PnL.
- **«Цена раннего БУ»**: для breakeven-выходов — дошла ли цена ПОСЛЕ выхода до TP1/TP2/TP3 в пределах `horizon_n`?
- Доля сделок, которые сначала шли −1/−2 шага (стоп-хант) перед плюсом.

**Ожидаемые выводы (на реальных данных пользователя):**
- Много breakeven + последующие TP2/TP3 (упущенная прибыль).
- Часть стопов до БУ (стоп-хант).
- Много timeout после сильного движения.

## 2. Grid-search с защитой от переобучения (Этап 2)

Скрипт: `python -m scripts.grid_search_gbp`

**Сетка (как в ТЗ):**
- `stop_mult`: [2.0, 2.5, 3.0]
- `breakeven_trigger_atr`: [0.5, 0.7, 1.0]
- `tp2_mult`: [2.0, 2.5, 3.0]
- `tp3_mult`: [3.0, 4.0, 5.0]
- `min_confidence_to_alert`: [0.80, 0.85, 0.88]
- `horizon_candles_n`: [36, 48, 72]

**Двухэтапно**:
1. Грубая (stop × BE × tp3) — 27 прогонов.
2. Уточнение (conf × horizon) на лучших.

**Критерии выбора (строгие, НЕ total PnL):**
- primary: `median PF > 1.0`
- secondary: число плюсовых фолдов (из 24)
- ≥10 сделок / фолд
- **Отложенная проверка**: последние ~6 фолдов (2024–26) — кандидат обязан ≥4/6 плюсовых (эти фолды не участвуют в выборе).

Вывод: `logs/grid_search_gbp.csv` + топ-5.

## 3. Кандидаты (Этап 3)

### 3a. v4a «Трендовая» (только конфиг)
```yaml
assets.GBPUSD.signal_grid:
  stop_mult: 3.0
  breakeven_trigger_atr: 1.0
  tp2_mult: 2.5
  tp3_mult: 4.0
```

### 3b. v4b «Трейлинг-раннер» (код + конфиг)
- `trailing_atr_mult: 2.0` (после TP1+TP2 остаток 20% трейлится)
- `stop_mult: 3.0`, `breakeven_trigger_atr: 1.0`

Реализовано в:
- `config/loader.py::get_signal_grid` (ключ `trailing_atr_mult`)
- `model/ensemble_backtest.py` (логика трейлинга + exit_reason="trailing")
- `execution/mt5_trader.py` (трейлинг после TP2)

### 3c. v4c «H4»
```yaml
timeframe: H4
labeling.horizon_candles_n: 24
signal_grid: {stop_mult: 3.0, breakeven_trigger_atr: 1.0}
```
Бэкфилл: `python -m scripts.backfill_data --all --timeframe H4 --start 2022-01-01 --end 2026-08-06`

## 4. Per-asset модельные флаги (Этап 4)

```yaml
assets.GBPUSD.model:
  use_regime_feature: true
  include_zero_class: true
```

Поддержка:
- `scripts/run_backtest.py::merge_asset_cfg` (теперь поддерживает `"model"`)
- `realtime/pipeline.py` → `effective_cfg["model"]`
- `scripts/retrain_with_real_trades.py` (читает per-asset `model`)

Дополнительно (без кода):
- `ensemble.suppress_regimes: [compression, reversal_watch, range]` (только тренды)
- Волатильностный фильтр (`atr_percentile > 90`) — через news guard / BoE/CPI если возможно.

## 5. Критерий успеха (финальный)

На `logs/backtest_gbpusd.csv` (2022–26, 24 фолда H1):

**Принимаем, если ВСЕ:**
- `expectancy > 0`
- `PF (median) > 1.0`
- плюсовых фолдов ≥ 10/24
- плюс на отложенных 2024–26 (≥4/6)

**Иначе:**
- `assets.GBPUSD.enabled: false`
- В PR описать перепробованное и предложить новые данные (кросс GBP/EUR, COT, ставки BoE) отдельным ТЗ.

## 6. Команды (пользователь)

```powershell
python -m pytest -q -p no:cacheprovider

python -m scripts.diag_gbp_profile
python -m scripts.grid_search_gbp

# (опционально) H4
python -m scripts.backfill_data --all --timeframe H4 --start 2022-01-01 --end 2026-08-06

python -m scripts.train_all_assets
python -m scripts.run_backtest --asset GBPUSD
```

## 7. Тесты

- Существующий набор зелёный (цель ≥239 passed).
- Добавлены:
  - `trailing_atr_mult` в `get_signal_grid` (None по умолчанию)
  - `EnsembleBacktester` с `trailing_atr_mult` → exit "trailing" (синтетический тест)
  - per-asset `model`-мерж в `merge_asset_cfg` и `effective_cfg`
  - smoke-тесты diag/grid-search на мок-данных (`scripts/tests/test_gbp_fix_smoke.py`)

## 8. Документация / Change Log

- `docs/benchmarks.md` (Change Log)
- `docs/GBP_FIX_STRATEGY.md` (этот файл)
- `TODO.md` / `docs/TODO.md` — обновить счётчик

## 9. Ограничения (соблюдены)

- XAU/XAG/BTC/EURUSD — без изменений.
- Глобальные секции — без изменений (кроме per-asset model merge).
- `alerts/`, `realtime/app.py`, `model/ensemble.py` — без изменений (кроме trailing в loader).
- Не трогаем `.env`, секреты, `output/`.

**Статус на момент коммита:** код, конфиг, тесты, скрипты готовы. Реальные цифры — после прогона пользователем на его MT5 БД.
