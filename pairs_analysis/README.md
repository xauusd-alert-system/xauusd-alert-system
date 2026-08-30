# pairs_analysis — парный статистический анализ (Этап 1: ядро)

Оценивает статистическую связанность пар инструментов (коинтеграцию),
считает набор метрик и готовит базу для торговых сигналов
mean-reversion / no-edge (этап 2). ТЗ: `pairs-trading-module.md`.

## Что умеет (Этап 1)

- **Данные** (`data.py`): MT5 sqlite (`ohlcv_m5/m15/h1`), CSV-импорт,
  публичный Binance REST (без ключей) с кэшем в `data/pairs_cache.sqlite`;
  ресемплинг до H1/H4/D1; выравнивание пар по времени (inner join).
- **Метрики** (`metrics.py`, ТЗ §4.1–§4.2):
  - β: фильтр Калмана (динамический, point-in-time) + fallback OLS (90);
  - лог-спред `e_t = ln(P1) − β·ln(P2)`;
  - z-score по окну 90 (μ, σ скользящие);
  - ADF p-value (statsmodels);
  - half-life по OU (θ из регрессии Δe = −θ·e, HL = ln2/θ, в барах и днях);
  - σ спреда (оконная + годовая), текущий ratio цен;
  - Math Board: Hurst (R/S по приращениям), skew, excess kurtosis, ACF(1),
    realized vol.
- **`PairAnalyzer`** (`analyzer.py`): конфиг пары → `PairMetrics`
  (все ряды + сводка `summary()` для дашборда).

## Использование

```python
from pairs_analysis import load_config, PairAnalyzer

cfg = load_config()  # config/pairs_config.yaml
for pair in cfg["pairs"]:
    m = PairAnalyzer(pair, cfg["analysis"]).analyze()  # D1 по умолчанию
    s = m.summary()
    print(
        s["name"],
        "β=",
        s["beta"],
        "ADF p=",
        s["adf_p"],
        "HL дн=",
        s["half_life_days"],
        "z=",
        s["z"],
        "Hurst=",
        s["hurst"],
    )
```

CLI-проверка всех пар:

```bash
python -m pytest tests/test_pairs_analysis.py -v
```

## Конфиг (`config/pairs_config.yaml`)

- `pairs`: имя, `source` (`mt5` | `binance` | `csv`), `symbols`, таймфреймы.
- `analysis`: окно z/σ (90), окно OLS (90), `kalman_q/r` (адаптация β),
  `adf_p_max`, `half_life_range_days`, пороги Херста.
- `thresholds`: `entry_z` 2.0, `exit_z` 0.0, `stop_z` 3.0 (этап 2).

Примеры:

| Пара | source | symbols | Данные |
|---|---|---|---|
| XAU/XAG | mt5 | XAUUSD, XAGUSD | metals, с 2024 (ресемпл из M15) |
| EURUSD/GBPUSD | mt5 | EURUSD, GBPUSD | FX, с 2024 |
| BTC/ETH | binance | BTCUSDT, ETHUSDT | 24/7, кэш sqlite |
| BTC/SOL | binance | BTCUSDT, SOLUSDT | 24/7, кэш sqlite |

CSV: `source: csv` + `paths: {SYM: путь}` (колонки с гибкими именами).

## Примечания

- **Point-in-time**: β_t из фильтра Калмана и скользящие окна используют
  только данные до бара t — без look-ahead (ТЗ §7.2).
- **Hurst считается по приращениям спреда**: R/S по уровням даёт ложные
  H>0.5 для mean-reverting рядов (артефакт короткой памяти, Lo 1991);
  по приращениям H<0.5 = mean-reverting, H≈0.5 = случайное блуждание,
  H>0.5 = персистентный/трендовый режим (ТЗ §4.2).
- Полный пересчёт 4 пар на D1 < 5 сек.

## Дорожная карта

- [x] Этап 1: данные + core-метрики + unit-тесты
- [ ] Этап 2: SignalEngine (NO EDGE / MEAN-REV LONG / SHORT, выходы) + бэктест
- [ ] Этап 3: ensemble (OU, Kalman trend, GARCH, GBM MC, Heston, Bayes) + агрегация
- [ ] Этап 4: дашборд по референсу
- [ ] Этап 5: интеграции (сканер, риск, журнал)
