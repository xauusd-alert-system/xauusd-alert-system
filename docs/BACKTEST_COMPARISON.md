# Backtest Comparison: Before vs After Refactoring

**Дата:** 2026-08-28
**Ветка:** `refactor/master-plan`
**Merge-base (baseline):** `cb5ce46` («chore: tighten .gitignore»)
**Текущий HEAD:** `72c2577` (63 коммита рефакторинга, задачи 1–3, 6 ТЗ выполнены)

## Параметры прогона (идентичны для обеих версий)

| Параметр | Значение |
|---|---|
| Скрипт | `python scripts/run_backtest.py` |
| Актив | `XAUUSD` |
| Таймфрейм | `M15` (per-asset override из `config/config.yaml`) |
| Label event | `traded` (`--label-event traded`) |
| Cutoff | `--end-date 2026-08-08` (строго до locked hold-out) |
| Данные | `data/market_data_mt5.sqlite`, таблица `ohlcv_m15`: 64 381 свеча (2023-12-01 … 2026-08-25); после cutoff — 63 286 свечей, last bar 2026-08-07 20:45 UTC |
| Walk-forward | train 300d / test 50d / step 50d → 13 фолдов (6 валидных, 7 пустых) |
| Модели | Обучаются per-fold внутри walk-forward (XGBoost + calibrate_model), prod-модельные файлы не требуются и не затрагиваются |
| Journal | `--no-journal` (прогон не загрязняет `logs/trial_journal.csv`) |
| Env | Один и тот же Python/venv, одна и та же БД; baseline запущен через `git worktree add ../xau-baseline cb5ce46` (основное дерево не переключалось) |

## Результаты

Оба прогона дают **побайтово идентичные** файлы
`logs/backtest_xauusd_traded.csv` и `logs/backtest_xauusd_traded_fold_summary.csv`
(проверено `fc` — IDENTICAL).

| Метрика | Baseline (`cb5ce46`) | Current (`72c2577`) | Δ |
|---|---|---|---|
| Total PnL (сумма фолдов) | −959.22 | −959.22 | 0 |
| Trades (сумма) | 334 | 334 | 0 |
| Win Rate (взвешенный) | 55.09 % | 55.09 % | 0 |
| Median Profit Factor (valid) | 0.915 | 0.915 | 0 |
| Median Sharpe (valid) | −0.29 | −0.29 | 0 |
| Worst Max DD (valid fold) | −1509.18 | −1509.18 | 0 |
| Median Max DD (valid) | −185.45 | −185.45 | 0 |
| Позитивных валидных фолдов | 2/6 (33.3 %) | 2/6 (33.3 %) | 0 |
| Sign test p (one-sided) | 0.8906 | 0.8906 | 0 |

### Разбивка по фолдам (identical, valid folds only)

| test window (UTC) | trades | win_rate | PF | total_pnl | max_dd |
|---|---|---|---|---|---|
| 2024-10-26 → 2024-12-17 | 191 | 60.73 % | 1.04 | +181.39 | −826.04 |
| 2024-12-17 → 2025-01-01 | 20 | 60.00 % | 0.94 | −22.46 | −185.44 |
| 2025-01-01 → 2025-02-25 | 2 | 50.00 % | 0.89 | −5.78 | −54.96 |
| 2025-02-25 → 2025-04-14 | 5 | 100.00 % | 999* | +186.75 | 0.00 |
| 2025-04-14 → 2025-06-29 | 112 | 43.75 % | 0.70 | −1155.84 | −1509.18 |
| 2025-06-29 → 2025-08-13 | 4 | 25.00 % | 0.23 | −143.28 | −185.46 |

\* PF=999 — кодирование inf при отсутствии убыточных сделок в фолде.

## Почему получилось идентично

Рефакторинг в `refactor/master-plan` был **поведенчески нейтральным для
backtest-пути**: P0-фиксы (P0-1 TTL сигналов, P0-2 ATR sanity, P0-3 hash,
P0-5 circuit breaker) входят в **исполнительный** контур (`execution/`,
`risk/`, `mt5_adapter/`), а не в контур `scripts/run_backtest.py`
(`features/` → `labeling/` → `model/` → `EnsembleBacktester`).
Walk-forward бэктест не использует MT5-исполнение, TTL-протухание сигналов,
swaps-исключение circuit breaker'а или fingerprint-hash — поэтому
идентичность результата здесь ожидаема и подтверждает поведенческую
нейтральность рефакторинга именно на пересечённом коде.

## Что это НЕ доказывает (важные оговорки)

1. **P0-фиксы сознательно меняют live-исполнение.** Они не участвуют в
   `run_backtest.py`, поэтому их эффект здесь не виден и не должен был быть
   виден. Для оценки P0-эффектов нужен PaperDriver-прогон фиксированного
   сценария сигналов через execution-контур (см. «Рекомендации»).
   Механика ожидаемого влияния по ТЗ:
   - **P0-1 TTL** (`execution.signal_ttl_ms`, default 2h, M15 → 6h):
     сигналы старше TTL не исполняются → меньше протухших входов,
     потенциально другой набор live-сделок vs. исторический 24h-дефолт.
   - **P0-2 ATR sanity** (`execution.entry_grid`): сигналы с аномальным
     ATR (NaN/<=0/экстремум) отклоняются → меньше сделок с вырожденной
     геометрией стопа/тейка.
   - **P0-5 circuit breaker** (`risk.circuit_breaker.exclude_swaps: true`):
     свопы больше не триггерят daily-loss блокировку → меньше ложных
     остановок торговли на положительно/отрицательно-своповых неделях.
2. **Валидные фолды в этом прогоне слабые** (2/6 позитивных, Total PnL
   отрицательный). Это состояние исследовательской конфигурации XAUUSD на
   M15/traded, известное до рефакторинга (см. `logs/trial_journal.csv`,
   прогоны 2026-08-13…08-25 с total_pnl от −396 до −10115) — оно не
   ухудшено и не улучшено рефакторингом.
3. `data/backtest/*.json` (`equity_path.json`, `pairs_backtest_report.json`)
   — результаты **другой природы** (pairs-сканер по US-акциям), не
   XAUUSD-walk-forward, поэтому как reference не использовались.

## Выводы

- Поведенческая нейтральность рефакторинга на backtest-пути подтверждена
  побайтовой идентичностью результатов: 13 фолдов, все метрики до десятого
  знака совпадают.
- Обучение моделей (XGBoost + calibration, purge_gap=36) детерминировано
  воспроизвелось в обеих версиях — рефакторинг `model/trainer.py`,
  `model/ensemble_backtest.py`, `features/` не изменил численный результат.
- P0-фиксы остаются вне зоны данного сравнения по построению; их влияние —
  тема execution-level-тестов (см. ниже).

## Рекомендации

1. Для оценки P0-1/P0-2/P0-5 прогнать **PaperDriver-сценарий** с
   фиксированным набором сигналов (в т.ч. протухшие и с аномальным ATR)
   через execution-контур до/после и сравнить intent→fill-статистику.
2. Повторить это сравнение после любых изменений в `backtest/`, `features/`,
   `model/`, `labeling/` — regress-гейт «два прогона, `fc` == IDENTICAL»
   занимает ~4 мин и ловит любые численные дрейфы.
3. Не интерпретировать отрицательный Total PnL этого прогона как регрессию:
   валидные фолды и их знак идентичны baseline и историческим прогонам.

## Воспроизведение

```
# baseline (в worktree, не трогая основное дерево):
git worktree add ../xau-baseline cb5ce46
cd ../xau-baseline
python scripts/run_backtest.py --asset XAUUSD \
  --db-path <repo>/data/market_data_mt5.sqlite \
  --label-event traded --end-date 2026-08-08 --no-journal

# current (в основном дереве):
python scripts/run_backtest.py --asset XAUUSD \
  --db-path data/market_data_mt5.sqlite \
  --label-event traded --end-date 2026-08-08 --no-journal

# сравнение:
fc logs\backtest_xauusd_traded.csv <baseline>\logs\backtest_xauusd_traded.csv
```

Сырые CSV обеих версий сохранены вне git в `results_baseline_*.csv`
и `logs/backtest_xauusd_traded.csv` (обе директории в .gitignore).

| Версия | HEAD | Дата прогона | Result |
|--------|------|--------------|--------|
| Baseline | cb5ce46 | 2026-08-28 | baseline |
| Current #1 | 72c2577 | 2026-08-28 | fc IDENTICAL |
| Current #2 | c8a113a | 2026-08-29 | fc IDENTICAL (после ruff format + E701/E702 fix) |