# Research Audit — Pairs / SMC / Order Book

**Дата:** 2026-08-17. **Ветка:** `arena/01a00a0d-xauusd-alert-system` (на момент написания HEAD `6aec115`; документ закоммичен 2026-08-17 по команде владельца, HEAD `4efa6c3`).
**Режим:** research-only / paper. Скриншоты — визуальный reference, не dataset.
**Статус:** аудит + research design; изменений кода, влияющих на execution, НЕТ.

---

## A. Repository audit

| Area | Current state | Evidence | Gap |
|---|---|---|---|
| **Pairs (XAU/XAG)** | Нет pairs-кода: нет hedge ratio, half-life, rolling ADF, pairs z-score, log-spread. ADF есть только в `features/fractional_diff.py` (выбор d для frac-diff, `adfuller`, autolag=AIC) — не pairs-инструмент. XAGUSD в конфиге SHADOW (`enabled: false`), XAUUSD флагман | grep: `cointegrat/hedge_ratio/half.life` — 0 совпадений в коде; `config.yaml:158-159`; `features/fractional_diff.py:66-90` | Полная отсутствует: нужны XAU+XAG causal-ряды, спецификация β, окна, ADF-политика |
| **SMC** | 5 параметров (Manipulation Index, Zone Strength, SMF Ratio, Liquidity Grab, Delta Confidence) — dashboard-report scalars в `features/smart_money_metrics.py`; потребители `/api/institutional-metrics` и Telegram `/metrics`; в ML-фичи НЕ входят (документировано в `docs/benchmarks.md`: «deliberately NOT vectorized»). Vol Filter отсутствует | `docs/POSITION_QUALITY_AUDIT.md` (полный аудит), grep | Нет per-row causal-серий; нет Vol Filter; нет gates/decision |
| **Order flow** | CVD, CVD slope, order_flow_imbalance_14/50, VWAP, dist_vwap_atr — в `features/order_flow.py`, входят в `FEATURE_COLUMNS` (46) train/backtest/realtime | `features/order_flow.py:10-102`, `docs/benchmarks.md` 2026-08-06 | Источник — OHLCV `tick_volume` (прокси), не реальный buy/sell flow |
| **MT5 DOM** | Отсутствует: нет `MarketBookAdd`/`OnBookEvent` в MQL5, нет `market_book_get/add` в Python, нет DOM-кода вообще | grep `market_book|OnBookEvent|book_add|book_get|order.?book` по `mql5/` и `execution/` — 0 | Доступность DOM у брокера не проверена (нет Windows-терминала в sandbox) |
| **GC source** | Отсутствует: нет CME/COMEX/Databento adapter, manifest'а, credentials | grep `comex|databento|GC` — 0 | Источник не подключён; нужен source manifest до интеграции |
| **Synthetic LOB** | `simulation/engine/order_book.py` (OrderBook) + `matching_engine.py` + synthetic agents/news — тестовый контур (MT5 shim) | `simulation/`, README «Simulation» | Роль ясна: только deterministic тесты; НЕ market evidence |
| **Cross-asset data** | Только EUR/GBP M15 CSV-экспорты (FxPro) + MT5 candles по настроенным активам; XAG — SHADOW; GC/DXY нет | `data/fx_m15_export/` | Нет выровненного cross-asset ряда (XAU/XAG/GC/DXY) |

## B. Feature classification

| Feature | Source | Provenance | Causal | Production-ready | Research-only |
|---|---|---|---|---|---|
| Manipulation Index | OHLCV (wick/fakeout/volume) | `ohlcv_proxy` (после аудита PQ — явный маркер) | да (window ≤ 20, тест no-lookahead) | нет (dashboard-only) | да |
| Zone Strength | OHLCV (swing/volume/touches) | `ohlcv_proxy` | да (window ≤ 50) | нет (dashboard-only) | да |
| SMF Ratio | OHLCV volume (large vs small bars) | `ohlcv_proxy` | да (window 30) | нет | да |
| Liquidity Grab | OHLCV (sweep wick) | `ohlcv_proxy` | да (window 30) | нет | да |
| Delta Confidence | OHLCV candle anatomy × volume | `ohlcv_proxy` | да (window 30) | нет | да |
| Vol Filter | **не существует** | — | — | — | гипотеза из ТЗ |
| Pairs Z-score | **не существует** | — | — | — | гипотеза |
| GC imbalance | **не существует** (нет GC source) | — | — | — | гипотеза |
| CVD / CVD slope / imbalance / VWAP | OHLCV `tick_volume` | `ohlcv_proxy` (в `FEATURE_COLUMNS`) | да (no-lookahead тесты) | да (в базовой модели) | — |

## C. Screenshot-derived patterns → статус

| Screenshot value | Использование | Статус |
|---|---|---|
| Z-score +1.175, entry ±2σ, «NO EDGE» | Pattern A — explicit abstention: | research hypothesis; threshold НЕ принят, требует preregistration на development period |
| Manipulation Index 10/10 | НЕ импортировать как константу; underlying formula есть (прокси-паттерн) | research-only |
| Zone Strength 75% | formula есть (прокси) | research-only |
| SMF 0.88 BULL | formula есть; это candidate directional proxy, НЕ «smart money buying» | research-only |
| Liquidity Grab 7/10 | formula есть (прокси-паттерн) | research-only |
| Delta Confidence LOW | кандидат abstention-state (LOW → no_trade) | research hypothesis, gate не включать |
| Vol Filter ACTIVE | formula отсутствует в репо | **UNKNOWN** — не использовать |
| Неподписанная series в price pane | formula/source/units неизвестны | **UNKNOWN** — не использовать (§23) |
| Watchlist XAU/XAG/GC/DXY | candidate context universe; НЕ расширять trading universe | только контекст; для каждого актива нужен source contract |

## D. Pairs research — что нужно (design, не реализация)

- **База:** XAUUSD + XAGUSD оба в конфиге (XAG shadow, данные собираются через MT5 при enabled/backfill); sandbox-БД пустая (0 байт) — реальные ряды только на Windows-хосте.
- **Формальные определения (кандидаты):** `log_spread_t = log(XAU_t) − β_t·log(XAG_t)`; β — динамический (Kalman: state-space spec, process/observation noise, init, update timing, missing-data policy — всё зафиксировать до использования); `z_t = (spread_t − rolling_mean_t)/rolling_std_t` — causal rolling window, min obs, zero-std guard, outlier policy.
- **ADF:** только диагностика sample/window (`adfuller`, autolag=AIC, указать null hypothesis, window, frequency, multiple-testing treatment); rolling ADF/rolling β/rolling z-distribution; НЕ доказательство future cointegration.
- **Half-life:** диагностика скорости, НЕ гарантия возврата за N дней.
- **Policy:** `|z| < threshold → NO EDGE / stand_aside`; threshold versioned, preregistered, выбран только на development data, заморожен до forward evaluation.
- **Запрет:** pairs research не создаёт XAUUSD/XAGUSD trade автоматически; execution geometry не менять.

## E. Order book — вывод

1. **Реального DOM-источника в репозитории нет.** Ни MQL5 (`MarketBookAdd`/`OnBookEvent`), ни Python (`market_book_get`). Доступность MT5 DOM у брокера **эмпирически не проверена** (нет Windows-терминала в sandbox).
2. **Synthetic LOB (`simulation/`)** — только deterministic tests / state-machine / stress; запрещён как market evidence и в ML-feature path.
3. **GC (COMEX) ≠ XAUUSD CFD (FxPro GOLD).** GC — кандидат exogenous microstructure; до использования обязателен mapping (contract, roll, session, timezone, tick size, price scale, latency, timestamp alignment, market hours) + source manifest (`provider/dataset/instrument/contract/schema/timestamp_semantics/historical/live/license/credentials` — credentials вне репозитория).
4. **Кандидатные DOM-фичи** (bid_ask_imbalance, microprice, depth imbalance, top-of-book, book_pressure, MBO add/cancel/trade dynamics) — только после подтверждённого real source; definition по фактической data schema.
5. **Read-only discovery path** (если DOM доступен): symbol/broker/account mode/timestamp/subscription status/levels/bid-ask/price/volume/type; никаких `OrderSend`. Отвечает на вопрос «что именно представляет собой DOM этого symbol у данного broker/account».

## F. Cross-source provenance

- XAUUSD CFD / COMEX GC / synthetic LOB / OHLCV tick_volume — **четыре разных класса**; никогда не смешивать и не маркировать один как другой.
- OHLCV proxy ≠ real trade flow; candle delta ≠ real buy/sell dominance; broker volume ≠ exchange-wide volume; synthetic LOB ≠ real L2.
- Evidence hierarchy (ТЗ §3) соблюдается: verified source > verified dataset > existing causal repo data > deterministic derived proxy > dashboard calc > screenshot > synthetic.

## G. Safety state

```text
execution guards unchanged       (enabled_assets=[], require_demo_account=true)
enabled assets unchanged         (XAUUSD флагман, XAG shadow, BTC/EUR/GBP как было)
TradeGroupSpec unchanged
TP/SL/BE unchanged
no live orders sent
MQL5 read-only observer unchanged
position_quality flags           (отсутствуют в config — fail-closed)
```

## H. Data boundary

```text
No tuning/selection on 2026-08-08+.
```

Проверено: в рамках этого аудита не выполнялось никакого tuning/selection; threshold ±2σ из скриншота НЕ принят; никакие формулы не подбирались по данным.

## I. Unknowns (не заполнять догадками)

- Реальная семантика broker DOM (FxPro GOLD) — неизвестна, требует Windows-проверки.
- GC↔XAU mapping — не определён (нет GC source).
- Historical DOM availability — неизвестна.
- MBO availability — неизвестна.
- Latency / contract roll / session alignment — неизвестны.
- License/access к CME/Databento — не проверены; provider НЕ подключён.
- Vol Filter formula и неподписанная series из скриншота — **UNKNOWN**.

## J. Changed files

**Нет изменений кода в этой задаче** (research audit; рабочие правки предыдущего аудита PQ остаются в working tree незакоммиченными: `features/smart_money_metrics.py`, `features/tests/test_smart_money_metrics.py`, `docs/POSITION_QUALITY_AUDIT.md`). Добавлен только этот документ.

## K. Test result

```text
pytest -q
839 passed, 11 warnings
```

(запущено в этом workspace; 11 известных warnings: Starlette deprecation, малые synthetic CSCV fixtures).

## L. Research manifest (до forward evaluation)

Любой будущий research branch обязан зафиксировать manifest до запуска на locked forward (`2026-08-08+`): feature_name / source_kind / source_symbol / timeframe / lookback / formula / normalization / as_of_semantics / missing_data_policy / quality / causal=true / future_data_allowed=false; для cross-asset — reference_symbol / mapping / session_alignment / latency_policy / roll_policy. Эксперименты — по §31/§32: baseline vs A (SMC metadata) vs B (SMC hard gates) vs C (SMC features) vs D (pairs) vs E (verified order book), one-change-at-a-time, preregistered; метрики — sample/signal/accepted/abstention/costs/PF/DD/tail/calibration/fold-stability + DSR/PBO/CSCV при достаточном sample.
