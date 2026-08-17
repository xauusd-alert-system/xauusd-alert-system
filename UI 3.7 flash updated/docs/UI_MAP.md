# UI MAP — Полная карта привязки компонентов UI к backend-источникам

## Архитектура: Node.js UI / Proxy Layer
> **ВАЖНО:** Node.js сервер (`server.ts`) **НЕ ЯВЛЯЕТСЯ** источником данных или бизнес-логики.
> `server.ts` выступает исключительно как:
> 1. Раздатчик статического интерфейса (`src/dashboardHtml.ts`).
> 2. Тонкий сквозной HTTP-прокси (`proxyToBackend`) на реальный Python FastAPI бэкенд (`realtime/app.py`).
>
> **Требуемый backend-процесс:** Для полноценной работы системы параллельно должен быть запущен реальный Python FastAPI бэкенд:
> ```bash
> uvicorn realtime.app:app --host 127.0.0.1 --port 8000
> ```
> Адрес конфигурируется через переменную окружения `BACKEND_BASE_URL` (default: `http://127.0.0.1:8000`).
> При недоступности Python бэкенда прокси честно возвращает `HTTP 503` с телом `{"available": false, "source": "backend_unreachable", "reason": "..."}` и никогда не подставляет синтетические данные.

Документ составлен в соответствии с требованиями ТЗ (Раздел 4 и Раздел 1.1) на основе прямого анализа предоставленного исходного кода:
- `realtime/app.py`
- `realtime/data_envelope.py`
- `contracts/signal_spec.py`
- `config/config.yaml`
- `data/ledger_events.py`
- `alerts/chart_renderer.py`

---

## 1. Сводная матрица маппинга компонентов

### 1.1. System Status / Header / Governance
* **UI компонент:** Верхний баннер статуса системы, режим работы, circuit breaker, статус MT5, стратегия и конфиг-хэш.
* **API endpoint:** `GET /api/status`, `GET /health`
* **Backend producer:** `realtime/app.py::get_status()`, `realtime/app.py::health()`, `alerts/status_commands.py`
* **Source (из envelope):** `realtime/app.py` → `payload["source"]` (`"mt5_account"` при подключённом MT5, иначе `"unavailable"`).
* **Mode:** `realtime/app.py` → `payload["mode"]` (`"live_verified"` при MT5 + live, иначе `"implemented_not_live_verified"`).
* **as_of:** `realtime/app.py` → `payload["as_of_utc"]` / `payload["as_of_utc_ms"]`.
* **Freshness:** `realtime/data_envelope.py::stamp()` → `payload["freshness_status"]` (`"fresh" | "stale" | "offline" | "waiting" | "error"`).
* **Маппинг полей:**
  - Deployment Mode: `config/deployment.py::deployment_mode()` → `payload["deployment_mode"]` (факт: `"research"`)
  - Data Mode: `realtime/app.py::DATA_MODE` → `payload["data_mode"]` (из `os.environ["DATA_MODE"]`, default `"mock"`)
  - Strategy Version: `config/config.yaml` → `strategy.version` (`"xauusd-system-v3-signalbar-2026-08-16"`)
  - Strategy Spec Hash: `realtime/pipeline.py` → `payload["strategy_spec_hash"]`
  - Config Hash: `realtime/pipeline.py` → `payload["config_hash"]`
  - Balance: `realtime/app.py` → `payload["balance"]` (при `available: false` строго `null`)
  - Equity: `realtime/app.py` → `payload["equity"]` (при `available: false` строго `null`)
  - Open Positions Count: `realtime/app.py` → `payload["open_positions_count"]` (число активных тикетов)
  - Execution Enabled Assets: `config/config.yaml` → `execution.enabled_assets` (факт: `[]` -> `EXECUTION DISABLED (deny-all)`)
  - Require Demo Account: `config/config.yaml` → `execution.require_demo_account` (факт: `true`)
  - Circuit Breaker: `realtime/app.py` → `payload["circuit_breaker"]`
  - Trading Paused: `realtime/app.py` → `payload["trading_paused"]`

---

### 1.2. Primary Signal Card (WHAT / DECISION)
* **UI компонент:** Главная карточка текущего сигнала (по умолчанию XAUUSD).
* **API endpoint:** `GET /signal?asset={asset}&n_candles=300`
* **Backend producer:** `realtime/app.py::get_signal()` → `realtime/pipeline.py::RealtimePipeline.generate_signal()`
* **Source:** `realtime/pipeline.py` / `contracts/signal_spec.py`
* **Mode:** `realtime/app.py::DATA_MODE`
* **as_of:** `contracts/signal_spec.py` → `SignalSpec.timestamp_utc` / `SignalResponse.generated_at`
* **Freshness:** вычисляется по возрасту `timestamp_utc` относительно времени клиента / сервера.
* **Маппинг полей:**
  - Signal ID: `contracts/signal_spec.py` → `SignalResponse.signal_id`
  - Bias (направление): `contracts/signal_spec.py` → `SignalResponse.bias` (`"long" | "short" | "no_trade"`)
  - Signal State: `contracts/signal_spec.py` → `SignalResponse.signal_state` (`"watch" | "armed" | "confirmed" | "rejected" | "expired" | "no_trade"`)
  - Actionable Decision: разделение `bias` и `signal_state`. Если `signal_state != "confirmed"`, статус строго **NO TRADE** (non-actionable).
  - Confidence: `contracts/signal_spec.py` → `SignalResponse.confidence` (число 0.0–1.0)
  - Regime: `contracts/signal_spec.py` → `SignalResponse.regime` (`"trend_up" | "trend_down" | "range" | "compression" | "reversal_watch"`)
  - Session: `contracts/signal_spec.py` → `SignalResponse.session` (`"asia" | "london" | "newyork" | "off_session"`)
  - Setup Timeframe: `contracts/signal_spec.py` → `SignalResponse.setup_timeframe` (XAUUSD: `M15`)
  - Context Timeframes: `contracts/signal_spec.py` → `SignalResponse.context_timeframes`
  - Expiry: `contracts/signal_spec.py` → `SignalResponse.expires_at_utc`
  - Entry Zone: `contracts/signal_spec.py` → `SignalResponse.entry_zone` (строго из payload `[min, max]`)
  - Invalidation (Stop Loss): `contracts/signal_spec.py` → `SignalResponse.invalidation` (строго из payload)
  - Targets (TP1, TP2, TP3): `contracts/signal_spec.py` → `SignalResponse.targets` (строго из payload)
  - Step: `contracts/signal_spec.py` → `SignalResponse.step`

---

### 1.3. Decision Reasons / Guards (WHY)
* **UI компонент:** Список подтверждений и вето, обоснование решения модели и ансамбля.
* **API endpoint:** `GET /signal?asset={asset}`
* **Backend producer:** `realtime/pipeline.py` → `model/ensemble.py`
* **Source:** `realtime_pipeline`
* **Маппинг полей:**
  - Reasoning Summary: `contracts/signal_spec.py` → `SignalResponse.reasoning_summary`
  - Confirmation Predicates: `contracts/signal_spec.py` → `SignalResponse.confirmation_predicates` (список пройденных гейтов)
  - Confirmed By: `contracts/signal_spec.py` → `SignalResponse.confirmed_by`
  - Confirmation Time: `contracts/signal_spec.py` → `SignalResponse.confirmation_time_utc`
  - Target Legs (Allocations): `contracts/signal_spec.py` → `SignalResponse.target_legs`

---

### 1.4. Asset Hierarchy & Multi-Asset Signal Matrix
* **UI компонент:** Матрица сигналов по всем активам и иерархия инструментов.
* **API endpoint:** `GET /api/matrix`
* **Backend producer:** `realtime/app.py::get_signal_matrix()`
* **Source:** `realtime/app.py` → `payload["source"]` (`"per_asset_realtime_pipeline"` или `"unavailable"`)
* **Mode:** `realtime/app.py` → `payload["mode"]` (при `DATA_MODE != "live"` строго `"unavailable"`)
* **as_of:** `realtime/app.py` → `payload["as_of_utc"]`
* **Freshness:** `realtime/app.py` → `sig["freshness_status"]` (`"offline"` при non-live)
* **Маппинг полей (для каждого актива XAUUSD, XAGUSD, BTCUSD, EURUSD, GBPUSD):**
  - Asset: `config/config.yaml` → `assets.<KEY>.display_name` и `mt5_symbol`
  - Status: `realtime/app.py` → `sig["status"]` (`"ok" | "error" | "unavailable"`)
  - Available: `realtime/app.py` → `sig["available"]` (`false` при ошибке или non-live)
  - Bias: `realtime/app.py` → `sig["bias"]` (при `available: false` строго `null`, никакого `"neutral"`)
  - Confidence: `realtime/app.py` → `sig["confidence"]` (при `available: false` строго `null`, никакого `0.50`)
  - Reason: `realtime/app.py` → `sig["reason"]` (при `status: "error"` отображается реальная причина сбоя)
  - Иерархия активов (из `config/config.yaml`):
    * Primary Asset: `XAUUSD` (`enabled: true`, `timeframe: M15`)
    * Execution Candidates (Shadow/Paper): `BTCUSD` (`trade_profiles.btc_m5_scalp_v1.validated: false`, статус `RESEARCH / PAPER-ONLY`)
    * Shadow Asset: `XAGUSD` (`enabled: false`, статус `SHADOW / OFF`)
    * FX Assets: `EURUSD`, `GBPUSD` (`timeframe: H1`, probe-only)

---

### 1.5. Dynamic Correlation Matrix
* **UI компонент:** Таблица парных корреляций закрытых баров (M5).
* **API endpoint:** `GET /api/correlation`
* **Backend producer:** `realtime/app.py::get_correlation_matrix()`
* **Source:** `realtime/app.py` → `payload["source"]` (`"mt5_closed_candle_returns"` или `"unavailable"`)
* **Mode:** `realtime/app.py` → `payload["mode"]` (`"live_verified"` или `DATA_MODE`)
* **as_of:** `realtime/app.py` → `payload["as_of_utc"]` / `payload["as_of_utc_ms"]`
* **Freshness:** `realtime/app.py` → `payload["freshness_status"]` (`"fresh" | "offline"`)
* **Маппинг полей:**
  - Available: `payload["available"]` (при `DATA_MODE != "live"` -> `false`, `reason: "real_market_data_required"`)
  - Assets: `payload["assets"]` (список активных колонок)
  - Matrix: `payload["matrix"]` (двумерный массив `float` корреляций Пирсона)
  - Aligned Returns Count: `payload["n_aligned_returns"]`

---

### 1.6. Candlestick Chart & Trade Overlays
* **UI компонент:** Векторный график японских свечей с наложением уровней входа, стопа и тейков.
* **API endpoint:** `GET /api/chart/{asset}`
* **Backend producer:** `realtime/app.py::get_asset_chart()` → `alerts/chart_renderer.py::ChartRenderer.render_svg_candlestick()`
* **Content-Type:** `image/svg+xml` (при успехе) или `JSON HTTP 503` (при ошибке / research)
* **Source Headers:** `X-Data-Source: mt5-closed-candles`, `X-Data-Mode: live-verified`, `X-As-Of-UTC`
* **Поведение при `DATA_MODE != "live"`:**
  - Сервер возвращает `HTTP 503` с JSON: `{"available": false, "source": "unavailable", "mode": DATA_MODE, "reason": "real_market_data_required"}`
  - UI обязан честно отобразить заглушку: «CHART UNAVAILABLE — Real market data required» с выводом причины, **без генерации случайных свечей**.

---

### 1.7. Position Quality (PQ)
* **UI компонент:** Модуль оценки качества исполнения и сопровождения позиции.
* **Статус в backend:** **НЕ РЕАЛИЗОВАН**.
* **Отображение в UI:** Блок с явной честной маркировкой:
  `POSITION QUALITY: UNAVAILABLE (not implemented in backend)`.
  *Запрещено:* строить PQ-score на стороне клиента.

---

### 1.8. Smart Money Concepts / Market Structure & Flow Proxies
* **UI компонент:** Блок диагностики рыночной структуры (OHLCV Proxy).
* **API endpoint:** `GET /api/institutional-metrics`
* **Backend producer:** `realtime/app.py::get_institutional_metrics()` → `features/smart_money_metrics.py`
* **Source:** `payload["source"]` (`"mt5_closed_candles:XAUUSD"` при live, иначе `"unavailable"`)
* **Mode:** `payload["mode"]` (`"live_verified"` при live)
* **as_of:** `payload["as_of_utc"]`
* **Маппинг полей:**
  - Manipulation Index: `payload["metrics"]["manipulation_index"]` (`score`, `max: 10`, `source_kind: "ohlcv_proxy"`, `lookback: 20`, `data_status`)
  - Zone Strength: `payload["metrics"]["zone_strength"]` (`score`, `max: 100%`, `source_kind: "ohlcv_proxy"`, `lookback: 50`)
  - SMF Ratio: `payload["metrics"]["smf_ratio"]` (`ratio`, `source_kind: "ohlcv_proxy"`, `lookback: 30`)
  - Liquidity Grab: `payload["metrics"]["liquidity_grab"]` (`score`, `max: 10`, `source_kind: "ohlcv_proxy"`, `lookback: 30`)
  - Delta Confidence: `payload["metrics"]["delta_confidence"]` (`level`, `source_kind: "ohlcv_proxy"`, `lookback: 30`)
  - Report Text: `payload["report_text"]` (готовый отформатированный текст отчёта)
  *Терминологический инвариант:* использовать только названия «SMC Diagnostics» / «OHLCV-Proxy». Запрещены формулировки «Real Order Flow», «L2 Imbalance», «DOM Confirmed».

---

### 1.9. Macro News & Sentiment Analyzer
* **UI компонент:** Блок макроэкономического контекста.
* **API endpoint:** `GET /api/sentiment`
* **Backend producer:** `realtime/app.py::get_sentiment()`
* **Source:** `payload["source"]` (`"unavailable"`)
* **Mode:** `payload["mode"]` (`"implemented_not_live_verified"`)
* **Freshness:** `payload["freshness_status"]` (`"waiting"`)
* **Маппинг полей:**
  - Available: `payload["available"]` (строго `false`)
  - Score / Bias / Confidence: строго `null`
  - Reason: `payload["reason"]` (`"no_live_news_source_configured"`)
  - Отображение: «News source not configured», без рисования фальшивых заголовков или фиктивного нейтрального фона.

---

### 1.10. MT5 Open Positions (Execution Truth)
* **UI компонент:** Таблица открытых позиций терминала MetaTrader 5.
* **API endpoint:** `GET /api/positions`
* **Backend producer:** `realtime/app.py::get_positions()` → `alerts/status_commands.py` (`positions_get()`)
* **Source:** `payload["source"]` (`"mt5_positions"` при связи, иначе `"unavailable"`)
* **Mode:** `payload["mode"]` (`"live_verified"` при MT5, иначе `"implemented_not_live_verified"`)
* **as_of:** `payload["as_of_utc"]` / `payload["as_of_utc_ms"]`
* **Freshness:** `payload["freshness_status"]` (`"fresh"` или `"offline"`)
* **Маппинг полей (каждая позиция):**
  - Ticket: `p["ticket"]`
  - Symbol: `p["symbol"]`
  - Direction: `p["direction"]` (`"buy" | "sell"`)
  - Volume: `p["volume"]` (в лотах)
  - Open Price: `p["open_price"]`
  - Current Price: `p["current_price"]`
  - Profit: `p["profit"]` (плавающий PnL в USD)
  - Stop Loss: `p["sl"]`
  - Take Profit: `p["tp"]`
* **Состояния:**
  - `available: false` → блок `UNAVAILABLE` + `reason` + `freshness: offline`. **Запрещено писать «0 позиций» при offline/loading!**
  - `available: true` и `positions: []` → валидное состояние «No open positions».

---

### 1.11. Closed Trade Metrics / Track Record
* **UI компонент:** Статистика закрытых сделок (owner request 2026-08-11).
* **API endpoint:** `GET /api/metrics?period={today|week|2week|month|3month|all}`
* **Backend producer:** `realtime/app.py::get_metrics()` → `alerts/status_commands.py::compute_deal_metrics()` (`history_deals_get`)
* **Source:** `payload["source"]` (`"mt5_history_deals"` при подключении, иначе `"unavailable"`)
* **Mode:** `payload["mode"]` (`"live_verified"`)
* **as_of:** `payload["as_of_utc"]`
* **Маппинг полей:**
  - Period / Label: `payload["period"]`, `payload["period_label"]`
  - N Trades: `payload["n"]`
  - Win Rate %: `payload["win_rate_pct"]`
  - Profit Factor: `payload["profit_factor"]`
  - Total PnL: `payload["total_pnl"]`
  - Avg Win / Avg Loss: `payload["avg_win"]`, `payload["avg_loss"]`
  - Max Drawdown: `payload["max_drawdown"]`
  - Max Consecutive Losses: `payload["max_consec_losses"]`
  - Expectancy: `payload["expectancy"]`
  - Best / Worst Trade: `payload["best_trade"]`, `payload["worst_trade"]`
  - При `available: false` → баннер «MT5 не подключён — реальных данных нет».

---

### 1.12. Monte Carlo Risk & VaR Engine
* **UI компонент:** Стресс-тестирование распределения PnL (1000 симуляций).
* **API endpoint:** `GET /api/monte-carlo`
* **Backend producer:** `realtime/app.py::get_monte_carlo()` → `backtest/monte_carlo.py::MonteCarloSimulator`
* **Source:** `payload["source"]` (`"trading_events.position_closed.realized_pnl"`)
* **Mode:** `payload["mode"]` (`"live_history"`)
* **as_of:** `payload["as_of_utc"]`
* **Маппинг полей:**
  - Available: `payload["available"]` (требует минимум 2 закрытых сделки в `trading_events`, иначе `false`, `reason: "at_least_two_closed_trades_required"`)
  - N Trades in Sample: `payload["n_trades"]`
  - VaR 95% (USD): `payload["var_95_usd"]`
  - VaR 99% (USD): `payload["var_99_usd"]`
  - CVaR 95% / 99%: `payload["cvar_95_usd"]`, `payload["cvar_99_usd"]`
  - Profit Probability %: `payload["profit_probability_pct"]`
  - Risk of Ruin %: `payload["prob_of_ruin_pct"]`
  - Max Drawdown Median / 95%: `payload["max_drawdown_median_pct"]`, `payload["max_drawdown_95_pct"]`

---

### 1.13. Paper Forward Accumulator Status
* **UI компонент:** Мониторинг накопления OOS сделок на paper-аккумуляторе.
* **API endpoint:** `GET /api/paper-status`
* **Backend producer:** `realtime/app.py::get_paper_status()` → `data/paper_ledger.py`
* **Source:** `payload["source"]` (`"paper_status"` или `"unconfigured"`)
* **Mode:** `payload["mode"]` (`"paper_frozen"`)
* **as_of:** `payload["as_of_utc"]`
* **Маппинг полей:**
  - Available: `payload["available"]`
  - Run ID: `payload["run_id"]`
  - Closed Trades Count: `payload["closed_trades_count"]`
  - Min Closed Trades: `payload["min_closed_trades"]` (порог для валидации)
  - Accumulation Status: `payload["status"]` (исходные outcome metrics во время накопления скрыты по ТЗ)

---

### 1.14. Owner Ledger & Audit Timeline (Audit Truth)
* **UI компонент:** Таймлайн проверенных событий исполнения (Owner-Only).
* **API endpoint:** `GET /api/ledger/events`, `WS /ws?token={token}`
* **Backend producer:** `realtime/app.py::ledger_events()`, `data/ledger_events.py::read_ledger_events()`
* **Source:** `payload["source"]` (`"ledger_events"`)
* **Mode:** `payload["mode"]` (`"demo"`)
* **Security:** Требует `Authorization: Bearer <LEDGER_OWNER_TOKEN>`. При отсутствии или неверном токене API возвращает `HTTP 403`.
* **Маппинг полей (каждое событие):**
  - Event ID: `e["event_id"]` (первичный ключ, защита от дублей)
  - Event Type: `e["event_type"]` (`"intent_created" | "deal_added" | "order_history_added" | "position_modified" | "execution_reconciled" | "health_heartbeat"`)
  - Producer / Source: `e["source"]`
  - Intent ID: `e["intent_id"]`
  - Asset Key: `e["asset_key"]`
  - Broker Symbol: `e["broker_symbol"]`
  - Fill Price / Requested Price: `e["fill_price"]`, `e["requested_price"]`
  - Filled Volume: `e["filled_volume"]`
  - Latency ms: `e["latency_ms"]`
  - Signature Valid: `e["signature_valid"]` (`1 = True`)
  - Received At UTC: `e["received_at_utc_ms"]`
  - Payload: `e["payload"]` (декодированный JSON)

---

### 1.15. Execution Quality & Provenance Audit
* **UI компонент:** Детализация проскальзывания/задержки и родословная группы ордеров.
* **API endpoint:** `GET /api/ledger/execution-quality`, `GET /api/provenance/{group_id}`, `GET /api/ledger/lifecycle/{intent_id}`
* **Backend producer:** `realtime/app.py::ledger_execution_quality()`, `realtime/app.py::provenance_audit()`, `realtime/app.py::ledger_lifecycle()`
* **Security:** Owner Bearer required.
* **Маппинг полей:**
  - By Precision Group: `spread_points`, `latency_ms` (p50, p90, p95, p99), `adverse_slippage_price_units`
  - Provenance Lineage: `group_id`, `geometry_hash`, `provenance_hash`, `market_snapshot_id`, `feature_snapshot_id`, `model_inference_id`, `profile_id`, `broker_snapshot_id`, `cost_snapshot_id`, `ledger_events`

---

## 2. Иерархия источников истины (Source of Truth)
В соответствии с `docs/COMPLIANCE_DISCLOSURE.md` и ТЗ:
1. **AUDIT TRUTH:** `data/ledger_events.py` и `trading_events` (неизменяемый хеш-чейн лог фактов).
2. **EXECUTION TRUTH:** MetaTrader 5 API (`account_info()`, `positions_get()`, `history_deals_get()`).
3. **PRESENTATION / CONTEXT:** Telegram публикации и Web UI (могут иметь сетевую задержку, не являются источником истины).
4. **RESEARCH PROXY:** `features/smart_money_metrics.py` (только OHLCV proxy, не L2/DOM).

---

Карта UI_MAP составлена строго по исходным контрактам и готова к использованию в качестве фундамента для компонентов интерфейса.
