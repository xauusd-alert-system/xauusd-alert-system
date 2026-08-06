# FX v3 — early breakeven + stop 2.0 + H1 for EURUSD / GBPUSD

> Пакет «FX v3» атакует хвост потерь в механике ВЫХОДА для убыточных FX-активов,
> не трогая работающие XAUUSD / XAGUSD / BTCUSD. Дата: 2026-08-06.

## 1. Контекст и почему предыдущие попытки не сработали

### Walk-forward бэктесты пользователя (его данные из MT5, на момент перехода)

| Актив | TF | WR | PF | expectancy | total PnL | плюсовых фолдов |
|---|---|---|---|---|---|---|
| XAUUSD | M5 | 72.2% | 1.07 | +0.10 | +904 | 19/41 |
| BTCUSD | M5 | 73.4% | 1.06 | +0.09 | +1518 | 24/42 |
| XAGUSD | M15 | 65.2% | 0.91 | +0.84 | +585 | 6/14 |
| EURUSD | M15 | 61.6% | 0.65 | −0.26 | −246 | 0/14 |
| GBPUSD | M15 | 66.3% | 0.73 | −0.24 | −270 | 2/14 |

### Почему «качество входов» не помогло

1. **FX var2 (tighten)** — per-asset `min_confidence_to_alert: 0.92`, `ev_threshold: 0.10`,
   `hard_divergence_veto: true` на M15 — перевеса НЕ дали: EUR exp −0.26 / PF 0.64 / 0/14;
   GBP exp −0.23 / PF 0.80 / 2/14. Количество сделок упало, метрики не изменились.

2. **Математика проблемы** — при WR 62–66% и сетке 1:3 (TP1 = 1×шаг, но SL = 3×шага)
   хвост потерь (−3×шага) примерно в 6 раз больше среднего выигрыша (+0.5×шага на 50%
   объёма при TP1). Даже 66% win rate не окупает сетку 1:3 — позиция убыточна ДО
   издержек. FX (EUR/GBP) на M15/H1 среднереверсивен: цена часто доходит до стопа
   −3 шага раньше, чем до цели.

**Вывод: проблема в механике ВЫХОДА, а не во входах.**

## 2. Состав пакета FX v3 (только EURUSD/GBPUSD)

| Параметр | Было (M15 var2) | Стало (FX v3) | Зачем |
|---|---|---|---|
| `timeframe` | M15 | **H1** | шире шаг → издержки меньше в % от цели |
| `signal_grid.stop_mult` | 3.0 | **2.0** | стоп ближе: убыток −2×шага вместо −3×шага |
| `signal_grid.breakeven_trigger_atr` | — (нет) | **0.5** | ранний безубыток: SL → entry после 50% дистанции до TP1, ДО TP1 |
| `labeling.horizon_candles_n` | 36 | **48** | метки под более медленный H1-горизонт |
| `ensemble.min_confidence_to_alert` | 0.92 | **0.85** | возврат к мягчеу барьеру (var2 не помог) |
| `ensemble.ev_threshold` / `hard_divergence_veto` | 0.10 / true | **убраны** (наследуют глобальные 0 / false) | фильтры входов заменены механикой выхода |

Глобальные секции (`ensemble:`, `market_data:`, `features:`, `regime:`, `model:`),
`alerts/formatter.py`, `model/ensemble.py` (EV gate), `realtime/app.py`,
`config/loader.py` (только добавлен ключ) — **не изменялись**. Блоки
XAUUSD/XAGUSD/BTCUSD в `config/config.yaml` — **без изменений**.

### Новый параметр `breakeven_trigger_atr`

- Семантика: доля дистанции до TP1, при которой стоп переводится в безубыток (entry)
  **до** срабатывания TP1.
- `1.0` (дефолт) = ровно старое поведение: BE только при TP1 (в `ensemble_backtest`
  бит-в-бит: `be_triggered ⇔ hit_tp1` при множителе 1.0).
- `< 1.0` (например `0.5`) превращает большинство «почти-стоповых» сделок в скретчи (~0).
- `0.0` — допустимое значение и НЕ трактуется как `None` (`get_signal_grid` перекрывает).

## 3. Где реализовано (три движка)

| Файл | Что добавлено |
|---|---|
| `config/loader.py::get_signal_grid` | нормализованный ключ `breakeven_trigger_atr` (дефолт 1.0), перекрывается топ-уровнем `signal_grid:` и per-asset `assets.<key>.signal_grid` |
| `model/ensemble_backtest.py` | `self.be_trigger_mult`; в `run()` блок раннего BE до блока TP1 (`be_triggered`); финальный выход: `exit_reason = "breakeven" if (tp1_hit or be_triggered) else "stop"` |
| `backtest/engine.py` | `self.be_trigger_mult`; в `run()` блок раннего BE перед расчётом `hit_target`/`hit_stop` (`open_position._be_triggered`); ярлыки выхода не менялись ("stop"/"target"/"timeout") |
| `execution/mt5_trader.py` | `self.be_trigger_by_symbol` (per-MT5-symbol множитель); в `check_and_move_breakeven()` ранний BE до частичных закрытий (только при `be_trigger < 1.0`, защита от повторов через `trade_data["be_done"]`) |

## 4. Команды пользователя для замера

```powershell
python -m pytest -q -p no:cacheprovider

python -m scripts.backfill_data --all --timeframe H1 --start 2024-01-01 --end 2026-08-06
python -m scripts.train_all_assets
python -m scripts.run_backtest --asset EURUSD
python -m scripts.run_backtest --asset GBPUSD
```

Результаты пишутся в `logs/backtest_EURUSD.csv` / `logs/backtest_GBPUSD.csv`
(усреднение по фолдам, как в `docs/benchmarks.md`).

## 5. Критерий решения

Оставляем актив, если **все три** условия по новым CSV:

- `expectancy > 0.0`
- `profit_factor > 1.0` (средний)
- плюсовых фолдов `>= 5 из 14`

Иначе — отдельным коммитом:

- `assets.EURUSD.enabled: false` / `assets.GBPUSD.enabled: false` (в `config/config.yaml`),
- правка `realtime/app.py`: список активов в `/api/matrix` и `/api/correlation` брать
  из `assets.*.enabled` (тесты `test_app.py` адаптировать под динамический список).

## 6. Статус

- Код, конфиг и тесты: готово, `244 passed` (было 240; +4 новых, 0 регрессий).
- Реальные бэктесты: **ожидается перезамер пользователем** (БД с данными у агента нет;
  таблицы базлайнов в `docs/benchmarks.md` не переписываются до перезамера).
