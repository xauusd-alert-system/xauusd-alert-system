# Position Quality — Repository Audit Report

**Дата:** 2026-08-17. **Ветка:** `arena/01a00a0d-xauusd-alert-system`.
**Статус:** research/paper audit. По явной команде владельца 2026-08-17 документ и wording/provenance-правки закоммичены в ветку PR #35 и влиты в master.

---

## A. Repository state

```text
branch:       arena/01a00a0d-xauusd-alert-system
commit:       6aec115 (feat: add provenance lineage and source freshness contracts)
working tree: 3 modified files (features/smart_money_metrics.py,
              features/tests/test_smart_money_metrics.py,
              docs/POSITION_QUALITY_AUDIT.md) — НЕ закоммичены
```

## B. Audit findings — главный вывод

**Position Quality implementation в этом репозитории НЕ СУЩЕСТВУЕТ.**

ТЗ описывает систему (canonical `features/position_quality.py`, `ParameterResult`,
composite score, hard gates, Telegram/MT5 parity, optional feature branch), которой
в репозитории нет. Проверено:

| Проверяемое | Факт |
|---|---|
| `features/position_quality.py` | **ABSENT** |
| `tests/test_position_quality.py`, `test_position_quality_pipeline_contract.py` | **ABSENT** |
| `config.yaml: position_quality:` | **ABSENT** (ключа нет вообще) |
| `config.yaml: model.use_position_quality_features` | **ABSENT** |
| Vol Filter (шестой параметр) | **не существует нигде в репозитории** |
| `ParameterResult` / composite score / hard gates / `pass\|no_trade` decision | **не существуют** |
| Потребление шести параметров в pipeline/trainer/backtest/formatter/mt5_trader | **отсутствует** (grep: 0 ссылок) |

Шесть параметров (пять из шести) существуют **только** как dashboard-report scalars
в `features/smart_money_metrics.py` (Manipulation Index, Zone Strength, SMF Ratio,
Liquidity Grab, Delta Confidence), потребляются только `realtime/app.py
/api/institutional-metrics` и `alerts/control_bot.py /metrics`. `docs/benchmarks.md`
(2026-08-06) прямо фиксирует: «SMC report metrics deliberately NOT vectorized into
ML features — they are whole-frame report scalars for the dashboard, not per-row
causal series».

**Следствие:** большинство пунктов ТЗ (composite formula §14, hard gates §15,
Telegram/MT5 parity §20/§21, TradeGroupSpec invariant §22, feature parity §17,
baseline-46 §17) **неприменимы к текущему коду** — проверять нечего, ломать нечего,
включать нечего. Добавлять недостающую реализацию запрещено самим ТЗ (§29: не
добавлять новые компоненты без отдельной задачи; §34: research-фаза — отдельный
preregistered experiment).

### B.1 Матрица фактического состояния (до/после)

| Area | Before | After | Evidence |
|---|---|---|---|
| Producer | `features/smart_money_metrics.py` (5 scalars, dashboard-only) | то же; + `source_kind/lookback/data_status` per parameter | `compute_institutional_metrics` |
| Provenance | отсутствовала (никаких source markers) | `source_kind=ohlcv_proxy` на каждый параметр + aggregate `source_provenance` + disclaimer в report | новые тесты |
| Causality | `test_smart_money_metrics_no_lookahead` (pinned end, start varied, window ≤ 50) | без изменений; + regression: aggregate provenance не содержит frame-length (инвариант сохранён) | `features/tests/test_no_lookahead.py` |
| Train parity | нет PQ-ветки в train | n/a (ветки не существует) | grep |
| Backtest parity | нет PQ-ветки в backtest | n/a | grep |
| Realtime parity | нет PQ в pipeline | n/a | grep |
| Telegram parity | formatter не знает о PQ (render-only по построению) | без изменений | grep |
| MT5 gate | mt5_trader не знает о PQ | без изменений | grep |
| Geometry | PQ не может менять геометрию (не существует в execution path) | без изменений | grep |
| BE | не участвует | без изменений | grep |

### B.2 Выявленные и исправленные проблемы (минимальные)

| Severity | Finding | Fix |
|---|---|---|
| **P1 (provenance/honesty)** | Русскоязычные тексты отчёта утверждали институциональный контроль из OHLCV-прокси: «Полный институциональный контроль», «контролируют рынок на старших таймфреймах», «институционалы доминируют над розницей», «Умные деньги продолжают давить», «Крупные игроки продолжают активно работать», «сильная институциональная зона ликвидности», «Именно это объясняет резкие движения» | Переписаны на прокси-формулировки; добавлен `FORBIDDEN_CLAIMS` + regression test, запрещающий возврат таких формулировок (§5/§8/§12) |
| **P1 (provenance contract)** | У параметров не было `source_kind/as_of/lookback/quality` | Добавлены `source_kind=ohlcv_proxy`, реальный `lookback` (20/50/30/30/30), `data_status ∈ {sufficient, insufficient}`; aggregate `source_provenance` с note «NOT real trade flow / L2 / MBO / on-chain» (§5/§24) |
| **P2 (missing data)** | `calculate_delta_confidence` при <10 барах возвращал `MEDIUM` без маркера — missing data выглядела валидной | `data_status=insufficient` маркер в агрегате (уровень-дисплей остаётся, но явно помечен; gate'ов нет — execution impact отсутствует) (§12/§27) |
| **P3** | Нет disclaimer в отчёте | Добавлен «Источник: OHLCV-прокси…» в footer отчёта |

НЕ исправлено (и не должно быть исправлено): отсутствующий Vol Filter, отсутствующие
gates/decision/composite — их добавление запрещено §29; отсутствующие config-флаги
(см. E).

## C. Changed files (не закоммичены)

| file | reason | change | risk | tests |
|---|---|---|---|---|
| `features/smart_money_metrics.py` | P1 честность provenance | переформулировка 11 текстов + `SOURCE_KIND`/`PARAMETER_META`/`FORBIDDEN_CLAIMS` + `_provenance_keys()` + aggregate `source_provenance` + disclaimer | низкий: только текст и аддитивные ключи; скоринг-логика не тронута | +5 новых тестов |
| `features/tests/test_smart_money_metrics.py` | P2 regression coverage | 5 тестов: forbidden claims, source_kind/lookback/data_status, insufficient-маркеры, deterministic | низкий | 12 passed |
| `docs/POSITION_QUALITY_AUDIT.md` | отчёт | этот документ | — | — |

## D. Tests

```text
pytest -q
839 passed, 11 warnings
```

(было 834; +5 новых тестов в `features/tests/test_smart_money_metrics.py`; 11
известных warnings: Starlette deprecation, малые synthetic CSCV fixtures).

## E. Safety state

```text
position_quality.enabled            = ОТСУТСТВУЕТ в config.yaml (fail-closed по отсутствию)
model.use_position_quality_features = ОТСУТСТВУЕТ в config.yaml (fail-closed по отсутствию)
require_demo_account                = true   (config.yaml:558)
execution.enabled_assets            = []     (config.yaml:555, deny-all)
locked_holdout.start                = "2026-08-08" (config.yaml:625)
execution/mt5_trader.py             = единственный sender, не изменён
MQL5                                = read-only observer, не изменён
TradeGroupSpec / entry / SL / TP1-3 / allocation / BE / execution engine = не изменены
```

**Discrepancy (сообщается, не затирается):** ТЗ §16 ожидает `position_quality.enabled:
false` и `model.use_position_quality_features: false` в конфиге — в репозитории этих
ключей НЕТ. Отсутствие ключа безопаснее, чем `false`, но это расхождение между ТЗ и
репо; добавлять ключи вручную не стал (минимальность изменений, §31).

## F. Data boundary

```text
No tuning / feature selection / model selection on 2026-08-08+.
```

Проверено: в рамках этого audit ничего не настраивалось; единственные изменения —
текстовые формулировки и provenance-маркеры, не зависящие от данных. Никакие
thresholds/weights не менялись ни на каком периоде.

## G. Remaining unknowns (repository не позволяет доказать)

- Реальный торговый поток (real trade flow) — недоступен; все пять параметров —
  OHLCV-прокси.
- Exchange-wide volume — недоступен; используется broker/bar volume.
- L2 / order book / MBO / on-chain — недоступны; adapter'ов нет.
- Forward performance / OOS-валидация любых PQ-параметров — не установлена.
- Vol Filter (шестой параметр ТЗ) — не существует; семантика
  `min_relative_volume/min_volume_percentile/max_spread_to_tp1` нигде не реализована.
- Composite score (§14) и его weights — не существуют; формула из ТЗ не проверяема
  против кода.
- Hard-gate policy (§15) — не существует; «fake pass при missing data» невозможно
  в принципе (нет decision path), но и проверить gate'ы нечего.

## Вывод

Технический аудит завершён. Репозиторий находится в состоянии «Position Quality не
реализована» — что безопаснее, чем неполная реализация, и соответствует финальному
правилу ТЗ: «Не оптимизируй то, что ещё не доказано корректным». Следующий шаг — по
§34: preregistered research experiment (baseline vs metadata-only vs hard-gate vs
feature branch) после отдельного решения владельца. Изменения этого audit оставлены
в working tree для ревью; коммит/push не выполнялись.
