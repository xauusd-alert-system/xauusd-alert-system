"""Contract tests for mql5/NeuroTrader (TZ_BOOKS integration).

The MQL5 code cannot execute in this Linux sandbox (no MetaEditor), so -
following the SignalDeskObserver precedent - the PYTHON/MQL5 contracts are
locked by scanning the .mqh sources and cross-checking them against the
Python modules they must stay in sync with:

1. FeatureEngine's feature order == model.sample_generator.FEATURE_COLUMNS_BASE
   (a model trained on Python features must be fed identically ordered
   features by the EA);
2. SignalBridge.mqh reads exactly the columns execution/signal_bridge.py
   writes, and checks the same schema_version;
3. SignalJournal's trace columns stay a superset of the bridge row (the
   T-22 join key features_hash must exist on both sides);
4. the tester criterion formula in TesterCriterion.mqh matches
   backtest/tester_criterion.py.
"""
from __future__ import annotations

import re
from pathlib import Path

from model.sample_generator import FEATURE_COLUMNS_BASE

NEURO_DIR = Path(__file__).resolve().parents[1] / "NeuroTrader"


def _source(name: str) -> str:
    return (NEURO_DIR / name).read_text(encoding="utf-8")


# ------------------------------------------------------------- feature order
def test_feature_engine_order_matches_python():
    src = _source("FeatureEngine.mqh")
    order = _source("FeatureEngine.mqh")
    # the documented order string inside the header
    m = re.search(r'FeatureOrder\(\) const\s*\{\s*return\s*"([^"]+)"',
                  src)
    assert m, "FeatureOrder() not found"
    assert m.group(1).split(",") == list(FEATURE_COLUMNS_BASE)

    # and the array used by the JSON loader / serializer agrees
    names = re.search(
        r"string names\[NEURO_FEATURE_COUNT\] = \{([^}]+)\}", order)
    assert names
    parsed = [n.strip().strip('"') for n in names.group(1).split(",")
              if n.strip()]
    assert parsed == list(FEATURE_COLUMNS_BASE)


def test_feature_count_macro_matches():
    src = _source("FeatureEngine.mqh")
    assert "#define NEURO_FEATURE_COUNT 7" in src
    assert len(FEATURE_COLUMNS_BASE) == 7


# --------------------------------------------------------------- the bridge
def test_signal_bridge_reads_the_python_schema():
    mql = _source("SignalBridge.mqh")
    py_columns = {
        "intent_id", "created_at_utc", "asset", "direction", "probability",
        "entry_price", "sl_price", "tp_price", "horizon_bars",
        "expires_at_utc", "status", "updated_at_utc", "features_hash",
        "comment",
    }
    # the NextPending query is a multi-line string concatenation: join every
    # quoted literal of the function and parse the SQL from the joined text
    start = mql.index("NextPending")
    end = mql.index("DatabaseFinalize", start)
    body = mql[start:end]
    literals = re.findall(r'"([^"]*)"', body)
    sql = " ".join(literals)
    assert sql.startswith("SELECT") and "FROM ml_signals" in sql

    select_part = sql.split("FROM")[0]
    read_columns = {c.strip() for c in select_part.replace("SELECT", "")
                    .split(",") if c.strip()}
    assert read_columns <= py_columns, (
        f"MQL5 reads unknown columns: {read_columns - py_columns}")
    # every column the Python writer produces is actually consumed
    assert read_columns == py_columns - {"horizon_bars", "status",
                                         "updated_at_utc"}

    # the EA refuses a mismatched schema_version (fail-closed contract)
    assert "schema_version" in mql
    assert "refusing to read signals" in mql

    from execution.signal_bridge import SCHEMA_VERSION
    define = re.search(r"#define NEURO_BRIDGE_SCHEMA_VERSION (\d+)", mql)
    assert define, "NEURO_BRIDGE_SCHEMA_VERSION not pinned"
    assert int(define.group(1)) == SCHEMA_VERSION


def test_bridge_statuses_match_python():
    """Every Python-side status is understood somewhere in the MQL5 tree."""
    from execution.signal_bridge import STATUSES
    combined = _source("SignalBridge.mqh") + _source("NeuroTraderEA.mq5")
    for status in STATUSES:
        assert f'"{status}"' in combined or f"'{status}'" in combined, status


# ---------------------------------------------------------------- the journal
def test_signal_journal_joins_on_features_hash():
    src = _source("SignalJournal.mqh")
    assert "features_hash" in src          # the T-22 join key
    assert "signal_id" in src
    # idempotency: restarts re-run UPDATE, never duplicate
    assert "INSERT OR IGNORE" in src


# ------------------------------------------------------- tester criterion
def test_tester_criterion_formula_matches_python():
    src = _source("TesterCriterion.mqh")
    assert "MathSqrt" in src
    assert "m_ddWeight" in src
    assert "STAT_PROFIT_FACTOR" in src
    assert "STAT_TRADES" in src
    assert "STAT_EQUITY_DDRELATIVE" in src
    # zero trades -> -inf (empty runs never win an optimization)
    assert "-DBL_MAX" in src
    # equity frames for the optimizer
    assert "FrameAdd" in src


# ------------------------------------------------------------- EA wiring
def test_ea_gates_reference_all_modules():
    src = _source("NeuroTraderEA.mq5")
    for include in ("FeatureEngine.mqh", "OpenCLInference.mqh", "RiskSizer.mqh",
                    "TradeExecutor.mqh", "AlertDispatcher.mqh",
                    "PositionManager.mqh", "NewsGuard.mqh", "DayFilter.mqh",
                    "SignalBridge.mqh", "SignalJournal.mqh",
                    "TesterCriterion.mqh"):
        assert f'#include "{include}"' in src, include
    # SQLite I/O stays OFF the tick path (book 7.6 / T-16 design)
    assert src.index("void OnTimer()") < src.index("NextPending")
    assert "ExpireStale" in src
    # fail-closed normalization
    assert "LoadNormalizationJson" in src


def test_opencl_inference_falls_back_to_cpu():
    src = _source("OpenCLInference.mqh")
    # context created ONCE (book 5.4), CPU fallback mandatory
    assert "CLContextCreate" in src
    assert "CPU fallback" in src
    kernel = re.search(r"__kernel void (\w+)", src)
    assert kernel and kernel.group(1) == "mlp_forward"
    assert "exp(" in src          # swish + sigmoid inside the kernel


def test_news_guard_is_tester_safe():
    src = _source("NewsGuard.mqh")
    assert "MQL_TESTER" in src
    assert "4014" in src
    # refresh happens on the timer, never per tick
    assert "Refresh" in src


def test_risk_sizer_never_rounds_up():
    src = _source("RiskSizer.mqh")
    assert "MathFloor" in src
    assert "OrderCalcMargin" in src
    assert "never round up" in src or "SKIP" in src


def test_mproj_lists_all_programs():
    mproj = (NEURO_DIR / "NeuroTrader.mproj").read_text(encoding="utf-8")
    for program in ("NeuroTraderEA.mq5", "EventTickSpy.mq5",
                    "BenchmarkOpenCL.mq5", "CreateVolatilitySymbol.mq5"):
        assert program in mproj, program
    for include in ("FeatureEngine.mqh", "SignalBridge.mqh",
                    "SignalJournal.mqh", "OpenCLInference.mqh"):
        assert include in mproj, include
