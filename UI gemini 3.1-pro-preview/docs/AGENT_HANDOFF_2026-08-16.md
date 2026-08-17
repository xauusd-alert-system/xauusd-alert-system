# Agent handoff — xauusd-alert-system

**As of:** 2026-08-17  
**Working branch:** `arena/01a00a0d-xauusd-alert-system` (main tree)  
**HEAD at handoff (main):** `bdc29e0`  
**Security-patch worktree:** `/tmp/xau-task` @ `4efa6c3` (не запушен)  
**Base of Arena patch:** `master` `9a15ba8`  
**PR:** [#35 — Arena system patch](https://github.com/xauusd-alert-system/xauusd-alert-system/pull/35)  
**Last full test (main):** `835 passed, 11 warnings` (чистое PR-дерево)  
**Last full test (security tree):** `860 passed, 11 warnings`  
**Locked/live-forward outcomes read:** **NO**

> Этот файл — продолжение предыдущего handoff (состояние на 2026-08-16, HEAD
> `170b822`, 546 тестов). Ниже сохранена вся релевантная информация из старого
> handoff и добавлен полный контекст работ Arena-сессии 2026-08-16 → 2026-08-17.

---

## 1. Non-negotiable safety state

Текущий config fail-closed (не менялся на протяжении всей сессии):

```yaml
deployment.mode: research
retraining.enabled: false
retraining.schedule.enabled: false
execution.enabled_assets: []
execution.require_demo_account: true
```

- Пустой allowlist = deny-all. Старый BTCUSD allowlist не восстанавливать
  (его admission gate измерен до causal grid-parity correction и невалиден).
- Не включать automatic retraining, paper accumulation, demo execution или live
  execution без отдельного reviewed promotion commit.
- **Дополнение сессии:** оба trade-profile validation-gated:

  ```yaml
  trade_profiles.xau_m15_intraday_v1: validated: false, validation_status: pending_xau_validation, paper_only: true
  trade_profiles.btc_m5_scalp_v1:     validated: false, paper_only: true
  ```

  `validate_profile_gate()` → `GeometryRejected(PROFILE_NOT_VALIDATED)` до
  построения TradeGroupSpec; XAU-статус закреплён коммитом `bdc29e0` +
  regression test.

## 2. Why old results are invalid

Commit `89350d5` (до сессии) унифицировал geometry вокруг causal signal-bar step:

1. step frozen из закрытого signal bar;
2. fixed step/min/max clamps;
3. TP/stop multipliers применяются один раз;
4. traded labels, оба backtest-движка, paper и live setup используют этот контракт;
5. ATR завершённого entry bar больше не используется при next-open.

Старые XAUUSD `wide_trend_filtered` и старый BTCUSD gate — исторические.
`docs/CANDIDATE_WIDE_TREND_FILTERED.md` — `INVALIDATED BY GRID-PARITY CONTRACT; PAPER START BLOCKED`.

Не цитировать старые PF/PnL/DSR/null-control цифры как текущее evidence.

## 3. Arena patch — состав (8 коммитов на master)

| # | Commit | Содержание |
|---|---|---|
| 1 | `0c1c5e6` | **MQL5 observer wave**: read-only `mql5/SignalDeskObserver/` (нет OrderSend/CTrade), disk outbox + ack watermark, history reconciler, детерминированные `SignalIntent`/`ExecutionEvent` контракты (`contracts/execution_contracts.py`), provenance manifest (`data/provenance.py`, `scripts/build_provenance_manifest.py`), `data/ledger_bridge.py` + `POST /api/ledger/ingest`, `signal_journal.asset_key` |
| 2 | `64a1552` | **Web-UI honesty wave**: `realtime/data_envelope.py` freshness (fresh/stale/offline/waiting/error), no-fallback (нет $100k/neutral 0.50/random chart), `/api/control/*` → 501 (browser mutation controls отключены), `/ws` owner-only ledger stream, dashboard banner INTERNAL DIAGNOSTIC VIEW |
| 3 | `b35d335` | **TradeGroupSpec v1**: immutable spec + state machine (DRAFT…RECONCILED, BE_RETRY, терминальные), `execution/trade_geometry.py` (чистый движок: profiles, step, tick alignment, cost admissibility, gross R), `config.trade_profiles`, volume allocator (floor leg1/leg2, leg3 = остаток), group risk один раз, BE от actual fill, `execution/trade_group_executor.py` (paper), `data/trade_group_store.py` (restart-safe) |
| 4 | `eb00883` | **Follow-up**: явные direction-цепочки LONG (`SL<entry<TP1<TP2<TP3`) / SHORT (зеркально) вместо знаковой формулы; BE на все `apply_to` legs с modify+query; demo env gate fail-closed; parity helper `as_geometry_payload()` |
| 5 | `5ad8500` | **P1.5 demo MT5 execution**: `ExecutionIntent` (+ geometry-hash verification), `MT5BrokerContext` (fresh snapshots, tick-align-only, ORDER_GEOMETRY_INVALID), hedging/netting adapters, `MT5TradeGroupExecutor` (demo-only; live → `LiveExecutionForbidden`; account_mode unknown → reject), `execution/reconciliation.py` (deal-history evidence, orphan detection), actions idempotency (`trade_group_actions`) |
| 6 | `35f6bfb` | **P1.5.1**: partial-submission compensation (`PARTIAL_SUBMISSION → COMPENSATION_REQUESTED → COMPENSATION_CONFIRMED → FAILED`), `FAILED_WITH_OPEN_RISK` (non-terminal, reconciliation активна), netting volume ledger от ACTUAL broker volume, cumulative allocation (без double-close), hedging partial fills |
| 7 | `6aec115` | **P1.6 provenance**: `ProvenanceSpec v1` (source/sourceType/sourceId/mode/asOfUtcMs/observedAtUtcMs/freshness/dataHash/parentIds; legacy_unavailable), `CostSnapshot.status` observed/estimated/unavailable (`COST_DATA_UNAVAILABLE` блокирует геометрию), `geometry_hash` vs `provenance_hash`, ledger actor-vs-source колонки, `GET /api/provenance/{group_id}`, `scripts/verify_provenance.py` |
| 8 | `bdc29e0` | **XAU validation correction**: `xau_m15_intraday_v1: validated: false` + `pending_xau_validation` + `paper_only: true`; regression test (gate `PROFILE_NOT_VALIDATED`) |

**Тесты:** PR-дерево `bdc29e0` — `835 passed, 11 warnings` (local Python 3.11);
CI run #86 (merge ref, Python 3.12) — `835 passed, 81 warnings` (green).

## 4. PR #35 — статус

- Открыт: https://github.com/xauusd-alert-system/xauusd-alert-system/pull/35
- base `master`, head `arena/01a00a0d-xauusd-alert-system` (origin @ `bdc29e0`)
- 8 коммитов, 70 файлов, +15 082/−154; конфликтов нет; reviews/comments — нет.
- **Незакоммиченные PQ/research-файлы в PR НЕ входят** (4 файла, см. §8).
- Внешний review выявил 2 P1: (1) ingress не был signed (см. §7), (2) заявка
  «840 passed» не совпадала с CI (835). Оба отработаны.

## 5. Pre-merge audit + pre-push inspection

- **Pre-merge audit (read-only):** вердикт **SAFE TO APPLY** для 8 коммитов;
  REVIEW по `xau_m15_intraday_v1.validated: true` (без evidence) → исправлено
  `bdc29e0`; REVIEW по 4 незакоммиченным PQ-файлам (не смешивать с Arena patch).
  Проверено: guards не ослаблены, единственный geometry source
  (`as_geometry_payload`), risk once per group, ledger immutability
  (RAISE(ABORT) в `trading_events`/`ledger_events`/`ledger_intents`), MQL5
  read-only (static scan), XAU/BTC validation-gated.
- **Pre-push inspection:** к push — ровно 1 коммит (`bdc29e0`, 2 файла);
  4 PQ-файла остаются uncommitted; verdict READY TO PUSH → ветка запушена, PR #35 открыт.

## 6. Ранее завершённые работы (до сессии, сохранено из старого handoff)

### Target/training contract
- XAUUSD `labeling.event: traded`; `train_mt5.build_full_df` передаёт `asset_key`;
  `--end-date` обрезает raw до фич; schema-v2 metadata (asset/event/geometry/
  class balance/config hash/strategy identity/data period/OOS calibration);
  uniqueness weights в base+calibration; `retraining.real_trade_merge_enabled: false`.

### Costs and broker evidence
- Candle storage мигрирует `spread`/`real_volume` (nullable, COALESCE upsert).
- `execution_fills` фиксирует requested/fill price, volume, latency, slippage,
  retcode, rejection reason (+ в сессии добавлены `intent_id`/`precision`).
- `scripts.execution_cost_report` — эмпирические распределения.

### Frozen paper
- `data/paper_ledger.py`, `paper/accumulator.py`, `scripts/paper_accumulate.py`,
  `scripts/run_live_forward_validation.py`; event-sourced, idempotent,
  UPDATE/DELETE protected; manifest checks; `validation_read` marker;
  accumulation остаётся заблокированной (нет кандидата).

### Operational integrity / Dashboard / News / Trailing / Correlation
- `config/deployment.py`, `config/strategy_spec.yaml`, `contracts/signal_spec.py`,
  signal lifecycle `watch → armed → confirmed`; hash-chained
  `data/trading_event_ledger.py`; dashboard без fake данных; `DASHBOARD_CONTROL_TOKEN`;
  news fail-closed; trailing через causal ATR; correlation по UTC-aligned returns.

## 7. Strict signed ingress — security patch (variant C)

### 7.1 P1 finding (внешний review PR #35)
1. `/api/ledger/ingest` принимал bearer-only при отсутствии `LEDGER_INGEST_SECRET`
   (`signature_valid = True` по умолчанию) — не signed ingress.
2. Заявка «840 passed» в PR description ≠ CI (835) — 5 тестов из незакоммиченного
   PQ-файла; чистый прогон PR = 835.

### 7.2 Решение владельца
Вариант **C**: MQL5 observer → локальный loopback signing proxy → внешний HTTPS
ingest с обязательными bearer + HMAC-SHA256.

### 7.3 Реализация (`/tmp/xau-task`, commit `4efa6c3`, 12 файлов, +934/−84)
- **`realtime/app.py`** — порядок: secret configured (503) → bearer (401/403) →
  raw body → `X-Ledger-Signature` HMAC-SHA256 по exact raw body (constant-time,
  401) → schema (422) → upsert. `signature_valid=True` только после реальной
  проверки. Нет `LEDGER_ALLOW_UNSIGNED_INGEST`.
- **`data/ledger_bridge.py`** — `secret` обязателен (raise до POST);
  `load_bridge_config` fail-closed (URL/token/secret); `X-Ledger-Signature` всегда.
- **`scripts/run_observer_signing_proxy.py`** (новый) — bind строго `127.0.0.1`
  (host ≠ loopback → `ProxyConfigError`); `POST /v1/observer/ingest`;
  constant-time `OBSERVER_PROXY_TOKEN`; валидация envelope (producer=`mt5_observer`,
  account_mode demo/contest; real → reject); HMAC по exact raw bytes; remote
  bearer; 2xx только при remote 2xx; remote failure → 502; 1 MB limit; без
  secrets в логах; без offline fallback.
- **`mql5/SignalDeskObserver/ObserverEA.mq5`** — `InpProxyUrl`/`InpProxyToken`
  вместо `InpLedgerUrl`/`InpLedgerToken`; `IsLoopbackProxyUrl()` строго
  `http://127.0.0.1:<port>/v1/observer/ingest` (https/hostname/0.0.0.0/
  non-loopback/query → reject); observer не знает remote token/secret;
  read-only сохранён; real → INIT_FAILED.
- **Docs**: `.env.example`, `docs/LEDGER_BRIDGE.md`, `docs/MQL5_OBSERVER_PLAN.md`,
  `mql5/SignalDeskObserver/README.md` — строгий контракт, 3 класса секретов,
  WebRequest allow-list `http://127.0.0.1`, retry через durable outbox;
  инструкции «оставьте secret пустым» удалены.
- **Тесты (+25):** server (503/401/valid/duplicate/bearer-only), bridge
  (secret required, byte-exact), proxy (loopback, token, schema, exact-body
  HMAC, remote 2xx/non-2xx, no secrets в логах), MQL5 static (no trade calls,
  loopback-only URL).

### 7.4 P1 availability bug (найден при ре-ревью)
В `data/ledger_bridge.py` была завершающая запятая:

```python
headers["X-Ledger-Signature"] = sign_envelope(envelope, secret),
```

→ tuple `("abc123",)` → `requests.exceptions.InvalidHeader` до отправки (outage
signed delivery). **Исправлено** (запятая убрана) + regression test:
`isinstance(signature, str)` + `requests.Request(...).prepare()`.
Коммит пересоздан через `git commit --amend` → **`4efa6c3`**.

**Воспроизведение бага подтверждено:** `InvalidHeader: Header part (('abc123',)) ... must be of type str or bytes`.

### 7.5 Test results (чистый task tree, `4efa6c3`)
```text
pytest -q data/tests/test_ledger_bridge.py scripts/tests/test_observer_signing_proxy.py realtime/tests/test_ledger_ingest.py
→ 43 passed, 1 warning

pytest -q
→ 860 passed, 11 warnings in 105.20s
```

### 7.6 Статус
`4efa6c3` НЕ запушен (ждёт решения владельца). Полный diff:
`/home/user/bfe5699-amended-full.diff` (`git diff --binary bdc29e0..4efa6c3`,
1361 строк, APPLY CHECK PASS). MetaEditor compile и demo smoke — **NOT VERIFIED**.

## 8. Position Quality audit (`docs/POSITION_QUALITY_AUDIT.md`)

**Position Quality НЕ реализована в репозитории.** Отсутствуют:
`features/position_quality.py`, `tests/test_position_quality*.py`,
`config position_quality`, `model.use_position_quality_features`, Vol Filter,
`ParameterResult`, composite score, hard gates.

5 из 6 параметров — dashboard-report scalars в `features/smart_money_metrics.py`
(Manipulation Index, Zone Strength, SMF Ratio, Liquidity Grab, Delta Confidence);
потребители `/api/institutional-metrics` и Telegram `/metrics`; в ML-фичи НЕ входят.

**Минимальные исправления (не закоммичены, в main tree):**
- `features/smart_money_metrics.py`: переформулировка 11 текстов (убраны
  «институциональный контроль/умные деньги/крупные игроки» из OHLCV-прокси),
  `FORBIDDEN_CLAIMS` + regression, per-parameter `source_kind=ohlcv_proxy` +
  `lookback` + `data_status {sufficient, insufficient}`, aggregate
  `source_provenance`, disclaimer.
- `features/tests/test_smart_money_metrics.py`: +5 тестов (12 total).
- `docs/POSITION_QUALITY_AUDIT.md`: полный отчёт.

## 9. Research audit — pairs / SMC / order book (`docs/RESEARCH_PAIRS_SMC_ORDERBOOK.md`)

- **Pairs (XAU/XAG)**: базы нет (нет hedge ratio/half-life/pairs z-score/
  cointegration); ADF только в `features/fractional_diff.py` (выбор d).
  XAG — SHADOW (`enabled: false`). Формальные определения — research design;
  threshold ±2σ из скриншота НЕ принят.
- **SMC**: 5 параметров — dashboard-only OHLCV-прокси; Vol Filter отсутствует.
- **Order book**: реального DOM-источника НЕТ (ни MarketBookAdd, ни
  market_book_get); `simulation/` LOB — test-only; GC/COMEX/Databento отсутствуют;
  GC ≠ XAUUSD CFD (mapping/roll/session/latency не определены).
- Evidence hierarchy соблюдена; screenshot = только design reference.
- Изменений кода не было; добавлен только отчёт.

## 10. Selected strategy decision

Полностью систематическая торговля — только после promotion. `human_confirmed`
как fail-closed mode существует в контракте, но Telegram `/confirm` workflow не
строился. Текущий mode `research` — broker routing заблокирован.

## 11. Known limitations (do not misrepresent)

1. SignalSpec поддерживает произвольные legs, но execution/backtest policy — три legs.
2. Primary event ledger покрывает только события после деплоя этого кода.
3. Нет реального исторического news CSV.
4. Нет достаточного эмпирического broker sample для замены static costs.
5. Нет real-data barrier-vs-traded / MTF сравнения в sandbox.
6. Нет schema-v2 frozen candidate model/manifest.
7. Telegram HTML parsing консервативен, строки unlinked.
8. `hmm_classifier.py` — research-only GMM (legacy имя).
9. Fractional-diff/CUSUM/meta-model — deferred до валидного baseline.
10. `settings.py` из внешних заметок — мусор, отсутствует.
11. **MQL5 observer: компиляция в MetaEditor и demo smoke НЕ выполнены** (нет
    Windows/MT5 окружения) — только static inspection.
12. **`4efa6c3` (security patch) не запушен** — ждёт решения владельца.

## 12. Files to read first

1. `docs/POST_PULL_RUNBOOK.md`
2. `docs/PRODUCTION_TRAINING_CONTRACT.md`
3. `docs/STRATEGY_SPEC.md`
4. `docs/CANDIDATE_WIDE_TREND_FILTERED.md`
5. `docs/DASHBOARD_DISCLOSURE.md`
6. `docs/COMPLIANCE_DISCLOSURE.md`
7. `docs/DEEPSEEK_V4_PRO_MAX_REAUDIT.md`
8. `config/strategy_spec.yaml`
9. `config/config.yaml`
10. `docs/TRADE_GROUP_SPEC.md` (TradeGroupSpec v1 + follow-up + P1.5/P1.5.1/P1.6)
11. `docs/MQL5_OBSERVER_PLAN.md` (MQL5 observer wave)
12. `docs/LEDGER_BRIDGE.md` (signed ingress contract)
13. `docs/WEB_UI_HONESTY_AUDIT.md`
14. `docs/POSITION_QUALITY_AUDIT.md`
15. `docs/RESEARCH_PAIRS_SMC_ORDERBOOK.md`
16. `scripts/run_observer_signing_proxy.py` (security patch)

## 13. Relevant commit history

```text
# Main tree (arena/01a00a0d-xauusd-alert-system)
bdc29e0 chore: keep xau_m15_intraday_v1 unvalidated until evidence exists
6aec115 feat: add provenance lineage and source freshness contracts
35f6bfb fix: harden MT5 trade-group partial submission and volume reconciliation
5ad8500 feat: add demo MT5 TradeGroup execution and reconciliation
eb00883 fix: harden TradeGroupSpec direction, BE, and paper execution invariants
b35d335 feat: TradeGroupSpec v1 — normalized trade lifecycle, geometry engine, paper executor
64a1552 feat: web-UI honesty wave — no-fallback contract, disabled controls, ledger WS stream
0c1c5e6 feat: MQL5 observer wave — execution contracts, provenance manifest, ledger bridge
9a15ba8 Merge pull request #34 ... (master base)

# Security-patch worktree (/tmp/xau-task, не запушен)
4efa6c3 fix(security): require signed Signal Desk ingress via observer proxy

# До сессии (история старого handoff)
170b822 feat: add versioned strategy lifecycle and primary trading ledger
6b0822a chore: freeze retraining and execution pending geometry revalidation
89350d5 Reaudit target parity paper safety and dashboard data integrity
5253a7b test: update gate checks after IS->OOS informativeness rename
```

Ветка session-fixed: продолжаем только на `arena/01a00a0d-xauusd-alert-system`.

## 14. Exact next work

### A. Требует owner's real DB (из старого handoff)
После pull и зелёного теста — копия БД с обрезкой на locked boundary
(никогда `--allow-locked`):

```powershell
Copy-Item data/market_data_mt5.sqlite data/market_data_ab_20260815.sqlite
python -m scripts.run_backtest --asset XAUUSD --timeframe M15 --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 --label-event barrier
python -m scripts.run_backtest --asset XAUUSD --timeframe M15 --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 --label-event traded
python -m scripts.compare_mtf_references --asset XAUUSD --db-path data/market_data_ab_20260815.sqlite --end-date 2026-08-08 --output logs/mtf_reference_comparison.json
python -m scripts.deflated_sharpe --asset XAUUSD --db-path data/market_data_ab_20260815.sqlite --variants current,wide,wide_trend_filtered,null --end-date 2026-08-08 --historical-trials 738
```

Затем: barrier vs traded → MTF references → все gates → preregistration →
только после этого frozen candidate + paper accumulation.

### B. Открытые пункты сессии
1. **Решение владельца по push `4efa6c3`** (security patch) в ветку PR #35 и
   обновление PR description на фактические числа:
   `Clean PR worktree: 835 passed, 11 warnings (local Python 3.11)` /
   `Security task tree: 860 passed, 11 warnings` /
   `GitHub Actions merge-ref: 835 passed, 81 warnings (Python 3.12) — historical, re-run after merge`.
2. **4 незакоммиченных PQ/research-файла** — решение: коммитить отдельно или оставить.
3. **MetaEditor compile + demo smoke** observer-а — на Windows-хосте.
4. **XAU validation report** — до любого использования `xau_m15_intraday_v1`.
5. Research-фазы (§34 ТЗ PQ): preregistered эксперименты baseline vs
   metadata-only vs hard-gate vs feature branch vs pairs vs verified order book.

## 15. What must not happen next

- Не тюнить на locked/live-forward данных (`2026-08-08+`).
- Не запускать `run_live_forward_validation` раньше времени.
- Не включать BTC/XAU execution из исторических комментариев.
- Не включать scheduled retraining до выбора baseline.
- Не добавлять Transformer/Mamba/TimesFM/large neural ensemble.
- Не выдавать OHLCV order-flow proxies за L2/реальный flow.
- Не выводить performance из Telegram tags / unmatched messages.
- Не совмещать model/threshold change с deployment promotion.
- Не включать Position Quality / GC DOM / synthetic LOB как market data.
- Не ослаблять signed ingress (нет bearer-only/unsigned fallback).
- Не менять XAU/BTC validation gates, profiles, guards, hold-out.

## 16. Verification at handoff

```text
# Main tree (bdc29e0, чистое PR-дерево)
pytest -q: 835 passed, 11 warnings

# Security worktree (4efa6c3)
pytest -q: 860 passed, 11 warnings
pytest -q data/tests/test_ledger_bridge.py scripts/tests/test_observer_signing_proxy.py realtime/tests/test_ledger_ingest.py: 43 passed

# CI (GitHub Actions run #86, merge ref, Python 3.12)
835 passed, 81 warnings

python compileall: clean (security tree)
git diff --check: clean
```

Warnings — известные Starlette/httpx deprecation и малые synthetic CSCV fixtures.
