# Retrain GBPUSD with Adaptive Labeling — Comparison (2026-08-29)

Задача 4.1 (ограничена владельцем до GBPUSD). GBPUSD — единственный SUCCESS по
контракту [docs/ADAPTIVE_PREREGISTRATION.md](ADAPTIVE_PREREGISTRATION.md).
XAUUSD FAIL, EURUSD MARGINAL — не тронуты.

## Методология

1. **Train (research bundle):**
   `python scripts/train_mt5.py --symbol GBPUSD --timeframe H1 --output output/models/gbpusd_direction_model_adaptive.joblib --end-date 2026-08-08`
   при временно включённом `assets.GBPUSD.labeling: adaptive_holding=true,
   high=0.001039, mid=0.000715`. Конфиг возвращён сразу после обучения.
   - bundle: `bundle_schema_version=2`, `labeling.adaptive_holding=true`,
     `effective_config_sha256=2df7eb3090bf…`, trained_at 2026-08-28T23:05Z,
     rows_labeled_binary=16383, class_counts={0:8016, 2:6562, 1:1805}.
   - prod-бандл от 2026-08-26T23:55Z остался нетронут; backup
     `gbpusd_direction_model_prod_backup.joblib` (byte-identical проверен `fc /b`).
2. **Backtest A (prod model на месте):**
   `python scripts/run_backtest.py --asset GBPUSD --end-date 2026-08-08 --no-journal`
   → `results/gbpusd_prod_retest.csv`.
3. **Backtest B (adaptive model на месте):** тот же свап/прогон
   → `results/gbpusd_adaptive_retest.csv`.
4. Конфиг после шага 3: `git diff config/config.yaml` — пуст;
   prod-бандл восстановлен byte-identically.

## ВАЖНОЕ архитектурное ограничение (результат обоих прогонов ИДЕНТИЧЕН)

`scripts/run_backtest.py` НЕ скорит тестовые окна подменённым prod-бандлом.
Walk-forward **обучает модель заново на каждом train-окне** и сохраняет её во
временный файл (`strategy_fn_factory`, HIGH-11 комментарий: "walk-forward folds
must NEVER overwrite the production model file"). `model_path` из конфига
читается в `main()` (line 321), передаётся в фабрику, но внутри `strategy_fn`
используется только freshly-trained per-fold model. Поэтому:

- `results/gbpusd_prod_retest.csv` и `results/gbpusd_adaptive_retest.csv`
  **byte-identical** (`fc` → no differences): 0/11 positive folds,
  median PF 0.49.
- Это **не валидация adaptive-bundle**, а воспроизведение baseline
  `gbpusd_false3` из ADAPTIVE_BARRIER.md (там тоже 0/11, PF 0.49 — label event
  `barrier`, fixed labels, per-fold retrain).

## Сравнение с результатами ADAPTIVE_BARRIER.md

| Прогон | Labels в labeling | Модель | Positive folds | Median PF | Валидность |
|--------|-------------------|--------|----------------|-----------|------------|
| gbpusd_false3 (Задача 1.3, прогон A) | fixed | per-fold retrain | 0/11 | 0.49 | валиден как baseline |
| gbpusd_true3 (Задача 1.3, прогон B) | **adaptive** (конфиг) | per-fold retrain | 3/7 | 1.00 | валиден как сравнение labels |
| gbpusd_prod_retest | fixed | **prod bundle (игнорируется WF)** | 0/11 | 0.49 | = baseline, bundle swap no-op |
| gbpusd_adaptive_retest | fixed | **adaptive bundle (игнорируется WF)** | 0/11 | 0.49 | **не проверяет** adaptive-bundle |

## Вердикт: **NOT VALIDATED** (не confirm / не degrade / не identical)

- Запрошенный протокол "swap bundle → backtest" **не может** сравнить prod vs
  adaptive модель: текущий walk-forward игнорирует подменённый бандл.
- Корректное сравнение требует **walk-forward-инференс** (per-fold trained на
  fixed labels vs per-fold trained на adaptive labels) — это уже сделано в
  Задаче 1.3: прогон B (adaptive labels) SUCCESS, 3/7, PF 1.00, PnL −46.79
  против −3000.59.
- Одиночный full-sample retrain-бандл (`gbpusd_direction_model_adaptive.joblib`)
  обучен и сохранён как артефакт, но его out-of-sample качество не оценено в
  этом сравнении. Full-sample retrain + walk-forward-инференс — отдельная
  методологическая задача, текущий движок бэктеста её не поддерживает.
- Риск p-hacking: обучение full-sample бандла и подмена его в fixed-label
  WF бэктест давало бы некорректный тест (train/test leak + label mismatch:
  бандл обучен на adaptive labels, бэктест генерирует fixed labels).

## Deploy recommendation (ТОЛЬКО при отдельном OK владельца)

1. **Не деплоить** на основании текущих артефактов — валидного
   bundle-level сравнения нет.
2. Если владелец хочет двигаться дальше, путь:
   - реализовать/использовать walk-forward-инференс с сохранённым бандлом
     (или OOS-оценку full-sample бандла), затем
   - deploy через `scripts/deploy_guard.py` с явным OK владельца;
   - мониторинг positive folds на live-forward первые N дней (N ≥ 10 торговых);
   - rollback-план: `gbpusd_direction_model_prod_backup.joblib` на месте
     (byte-identical prod проверен); восстановление = copy обратно.
3. Альтернатива (уже валидированная): adopt adaptive **labels** для GBPUSD —
   но это меняет label space (contract_version bump) и требует retrain всех
   prod-моделей актива + полного walk-forward цикла — решение владельца.

## Артефакты

- `output/models/gbpusd_direction_model_adaptive.joblib` — research bundle (schema v2, adaptive labels) — оставлен как артефакт.
- `output/models/gbpusd_direction_model_prod_backup.joblib` — backup prod (byte-identical).
- `results/gbpusd_prod_retest.csv` / `results/gbpusd_adaptive_retest.csv` (не коммитятся).
- `results/gbpusd_false3.csv` / `results/gbpusd_true3.csv` — из Задачи 1.3 (не коммитятся).
