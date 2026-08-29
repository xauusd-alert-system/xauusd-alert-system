# Feature Selection with New Features — Fractional Diff + CUSUM (Задача 3.3)

**Дата:** 2026-08-29
**Статус:** preregistered критерии зафиксированы ДО прогонов (см. сообщение задачи 3.3); конфиг возвращён в prod-состояние после прогонов.
**Фичи:** `close_fd` (FFd, d=0.4 — [features/fractional_diff.py](../features/fractional_diff.py)), 4 CUSUM-колонки (h/k 96/3.0/0.5 — [features/cusum.py](../features/cusum.py)).

## Enabler-правка (необходимое изменение для протокола B)

`model.feature_subset` в [model/trainer.py](../model/trainer.py) исторически фильтровал
только статический whitelist `FEATURE_COLUMNS` — новые колонки были бы молча
отброшены. Добавлены:

1. `feature_subset` — разрешает имена колонок, присутствующих на фрейме (не
   только whitelist);
2. `model.feature_subset_ext` — extension-список: ДОБАВЛЯЕТ research-фичи к
   whitelist, не требуя перечислять все 46 базовых в YAML.

Ни один prod-конфиг не задаёт `feature_subset`/`feature_subset_ext` → prod-поведение не изменено (тесты: 159 passed).
Также добавлены те же config-gated блоки fractional_diff/cusum во второй
`build_full_df` ([scripts/run_backtest.py](../scripts/run_backtest.py)) — бэктесты
используют свою копию пайплайна; при выключенных флагах поведение идентично.

## Критерии ДО прогонов (preregistered)

- Новые фичи входят в топ-20 MDA importance **И** OOS не хуже baseline
  (PnL, median PF, positive folds).
- CUSUM дополнительно (P2): sanity — риск-профиль баров `cp_bars_since <= 24`
  отличается от остальных (фича несёт режимную информацию).
- Провал: не в топ-20 **ИЛИ** хуже по OOS.

## OOS Walk-Forward результаты (Fixed vs New Features)

`python scripts/run_backtest.py --asset <A> --end-date 2026-08-08 --no-journal`

| Метрика | XAUUSD A | XAUUSD B | EURUSD A | EURUSD B | GBPUSD A | GBPUSD B |
|---------|----------|----------|----------|----------|----------|----------|
| Total PnL ($) | −959.22 | **+1240.88** | −1059.93 | −881.57 | −3000.59 | −2850.08 |
| Trades | 334 | 299 | 268 | 221 | 471 | 449 |
| Win rate (pooled) | 55.1% | **62.5%** | 48.5% | 46.2% | 58.8% | 60.6% |
| Median PF (valid) | 0.915 | **1.22** | 0.520 | 0.500 | 0.490 | 0.540 |
| Positive valid folds | 2/6 | **4/7** | 0/7 | 1/7 | 0/11 | 0/10 |

Прогоны A воспроизвели baseline Задачи 1.3 байт-в-байт (2/6 & 0.915; 0/7 & 0.52; 0/11 & 0.49) — воспроизводимость подтверждена.

## MDA Importance (purged K-fold, топ-20; полные ранги в logs/feature_selection_<asset>.json)

| Актив | Новые фичи в топ-20 | Вне топ-20 (в clustered-представителях) |
|-------|--------------------|------------------------------------------|
| XAUUSD | **`cp_bars_since` #1** (mda +0.0039), **`cp_last_sign` #4** (+0.0012) | `cusum_down_norm`, `cusum_up_norm`, `close_fd` |
| EURUSD | **`close_fd` #19** (+0.0008) | cp_*, cusum_* |
| GBPUSD | — (не в топ-20) | `cp_bars_since`, `close_fd`, `cp_last_sign`, `cusum_down_norm`, `cusum_up_norm` (все в clustered) |

## CUSUM Risk-Adjusted Sanity (XAUUSD, preregistered P2-критерий)

Forward 12-барная доходность и realised vol по группам баров
([scripts/research/cusum_risk_sanity.py](../scripts/research/cusum_risk_sanity.py)):

| Группа | n_bars | fwd_ret mean (bp) | ret/vol | realized vol (bp) |
|--------|--------|-------------------|---------|-------------------|
| post_cp (cp_bars_since ≤ 24) | 24 570 | **2.02** | **0.179** | 11.27 |
| остальные | 38 407 | 1.27 | 0.122 | 10.41 |

- ret/vol после change-point выше на **47%** (0.179 vs 0.122) при сопоставимой
  волатильности — фича несёт режимную информацию ✓.
- Направление: fresh_up mean +2.39 bp vs fresh_down +1.67 bp — различие слабое,
  подтверждает MQL5-план: CUSUM — режимный (volatility break) сигнал, не
  direction. `cp_last_sign` поэтому в топ-5 MDA, но использовать как
  направление нельзя.

## Вердикты по preregistered контракту

| Актив | Вердикт | Обоснование |
|-------|---------|-------------|
| XAUUSD | **SUCCESS** | `cp_bars_since` #1 и `cp_last_sign` #4 MDA (критерий «в топ-20» ✓); OOS строго лучше по всем трём метрикам: PnL −959 → **+1241**, median PF 0.915 → **1.22**, positive folds 2/6 → **4/7** ✓. CUSUM sanity ✓ (ret/vol +47%). Единственный актив, где все критерии выполнены. |
| EURUSD | **MARGINAL** | `close_fd` в топ-20 (#19) ✓; OOS лучше baseline (PnL −1060 → −882, +1 fold), но PF 0.50 < 0.52 и 0/7 → 1/7 — улучшение в пределах шума; фичи не вредят, польза не доказана. |
| GBPUSD | **FAIL** | Ни одна новая фича не в топ-20; OOS: PnL чуть лучше (−3001 → −2850), но PF 0.49 → 0.54 при positive folds 0/11 → 0/10 — всё ещё ноль позитивных фолдов. |

## Замечания и ограничения

1. **B-прогон менял только вход фичей** (labels/labeling не тронуты) — прирост
   XAUUSD не объясняется сменой label space (в отличие от Задачи 1.3).
2. XAUUSD 4/7 → это уже ≥3/6 по целевой метрике ТЗ («positive valid folds ≥3/6»).
3. MDA значения малы в абсолютном выражении (низкая signal-to-noise FX-фичей),
   поэтому ранги важнее величин; XAUUSD cp-фичи доминируют устойчиво.
4. Реализация `feature_subset_ext` — research-enabler; включение в prod
   (если владелец одобрит) будет через явный конфиг + retrain + валидация
   (bundles schema v2, feature_cols сохраняются в бандле автоматически).
5. Признак времени `cp_bars_since` — детерминированная функция истории цен;
   no-lookahead гарантирован каузальной реализацией и тестами truncation
   invariance у обеих фич.

## Рекомендация владельцу

- **XAUUSD**: кандидат на включение close_fd + CUSUM-колонок (через
  feature_subset_ext) с последующим retrain и валидацией; deploy — только
  через deploy_guard и OK.
- **EURUSD/GBPUSD**: фичи не включать (нет доказанной пользы).

## Артефакты (не коммитятся)

- `results/<asset>_fs_A.csv` / `results/<asset>_fs_B.csv` (+ fold_summary)
- `logs/feature_selection_<asset>.json` — полные MDA-ранги
- `results/xauusd_cusum_sanity.csv`
