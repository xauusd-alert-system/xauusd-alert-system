# Adaptive Holding — Preregistration (контракт)

**Дата:** 2026-08-29
**Статус:** контракт, зафиксирован ДО research-прогонов Задачи 1.3.
**Источник порогов:** [docs/VOL_PCT_DISTRIBUTION.md](VOL_PCT_DISTRIBUTION.md) (Задача 1.1).

## Гипотеза

Адаптивный горизонт (`adaptive_holding=true`: halve при `vol_pct > p75`,
quarter при `vol_pct > p95`) улучшает walk-forward метрики против
фиксированного горизонта (36/48) при `adaptive_holding=false`.

## Пороги (per-asset, подтверждены владельцем)

| Актив  | adaptive_high_vol_pct (= p95) | adaptive_mid_vol_pct (= p75) |
|--------|-------------------------------|------------------------------|
| XAUUSD | 0.003283                      | 0.001976                     |
| EURUSD | 0.001061                      | 0.000680                     |
| GBPUSD | 0.001039                      | 0.000715                     |

Активация по построению: 25 % баров halve (`base // 2`),
5 % баров quarter (`base // 4`) на каждом активе.

## Активы и labeling-события

| Актив  | event        |
|--------|--------------|
| XAUUSD | `traded`     |
| EURUSD | `atr_scaled` |
| GBPUSD | `atr_scaled` |

## Метрики успеха

- **Primary:** positive valid folds ≥ **3/6** И median PF(valid) > baseline.
- **Secondary:** Total PnL > baseline.
- **Провал:** результат byte-identical ИЛИ хуже по primary и secondary.
- **Marginal (решение владельца):** primary не выполнен, но PnL улучшился.

## Протокол оценки

1. Команда прогона:
   ```
   python scripts/run_backtest.py --asset <A> --end-date 2026-08-08 --no-journal
   ```
   (для EURUSD/GBPUSD — без `--label-event`; для XAUUSD — event `traded`).
2. Прогон A: `adaptive_holding=false` → `results/adaptive_false2.csv`.
3. Прогон B: `adaptive_holding=true` + пороги из таблицы выше →
   `results/adaptive_true2.csv`. Конфиг меняется ТОЛЬКО на прогон B,
   затем возвращается (`adaptive_holding: false`).
4. **Пороги не корректируются после просмотра результатов** — этот
   документ является контрактом. Любое изменение порогов требует нового
   preregistration-документа.

## Оговорка

Пороги посчитаны по полной выборке до cutoff (2026-08-08) — допустимо для
research. В prod пересчитывать по train-данным или скользящим окном.
