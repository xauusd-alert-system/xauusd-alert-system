"""
No-look-ahead proof tests for features/.

Core methodology: for each feature function, compute the feature on the FULL
dataset, then recompute it on a TRUNCATED dataset (rows [0..i] only). If the
value at row i differs between the two computations, the feature is using
future data at row i - this is exactly the bug class we must prevent.

Run with: pytest features/tests/test_no_lookahead.py -v
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from data.ingestion import fetch_mock_candles
from features.candle_anatomy import candle_anatomy
from features.indicators import (
    atr,
    bollinger_width,
    build_all_indicators,
    ema,
    macd,
    rsi,
)
from features.mtf_confluence import merge_htf_feature
from features.structure import detect_structure

CFG = load_config()
SESSIONS = CFG["sessions"]


# ---------------------------------------------------------------------------
# Real-data mode (audit 2026-08-12, Layer 0 validation).
#
# By default every test in this file runs on deterministic synthetic candles,
# exactly as before. Setting XAU_TEST_DB to the MT5 SQLite database makes the
# SAME assertions run against real broker history instead, so the causality
# proofs can be re-verified on the data the model is actually trained on:
#
#   XAU_TEST_DB=data/market_data_mt5.sqlite pytest features/tests/test_no_lookahead.py -v
#
# Optional: XAU_TEST_SYMBOL (default XAUUSD), XAU_TEST_BASE_TIMEFRAME (default
# M5, used by the length-agnostic tests). A timeframe that is absent or too
# short in the database SKIPS the test that needs it instead of failing it.
# ---------------------------------------------------------------------------
REAL_DB = os.getenv("XAU_TEST_DB") or None
REAL_SYMBOL = os.getenv("XAU_TEST_SYMBOL", "XAUUSD")
BASE_TIMEFRAME = os.getenv("XAU_TEST_BASE_TIMEFRAME", "M5")

_REAL_CACHE: dict = {}


def _skip(reason: str):
    """Skip via pytest when available, otherwise raise a runner-readable marker."""
    try:
        import pytest
    except ImportError:
        raise RuntimeError("SKIP: " + reason)
    pytest.skip(reason)


def _real_candles(timeframe: str):
    """Load and cache one timeframe of real candles from the MT5 SQLite DB."""
    key = timeframe.upper()
    if key not in _REAL_CACHE:
        from data.storage import read_candles
        try:
            frame = read_candles(REAL_DB, key, REAL_SYMBOL)
        except Exception:
            frame = None
        if frame is not None and not frame.empty:
            frame = frame.sort_values("timestamp_utc").reset_index(drop=True)
        else:
            frame = None
        _REAL_CACHE[key] = frame
    return _REAL_CACHE[key]


def _candles(timeframe: str, n: int, end_ts: int = None, seed: int = 42):
    """n candles of `timeframe`: real history under XAU_TEST_DB, else synthetic."""
    if REAL_DB:
        frame = _real_candles(timeframe)
        if frame is None:
            _skip(f"{REAL_SYMBOL} {timeframe}: no rows in {REAL_DB}")
        if end_ts is not None:
            frame = frame[frame["timestamp_utc"] <= int(end_ts)]
        if len(frame) < n:
            _skip(f"{REAL_SYMBOL} {timeframe}: need {n} candles, have {len(frame)}")
        return frame.tail(n).reset_index(drop=True).copy()
    return fetch_mock_candles(timeframe, n_candles=n, sessions_config=SESSIONS,
                              end_ts=end_ts, seed=seed)


def _sample_df(n=300):
    return _candles(BASE_TIMEFRAME, n)


def test_ema_no_lookahead():
    df = _sample_df()
    i = 150
    full = ema(df["close"], 21)
    truncated = ema(df["close"].iloc[:i + 1], 21)
    assert np.isclose(full.iloc[i], truncated.iloc[i], rtol=1e-9)


def test_rsi_no_lookahead():
    df = _sample_df()
    i = 150
    full = rsi(df["close"], 14)
    truncated = rsi(df["close"].iloc[:i + 1], 14)
    assert np.isclose(full.iloc[i], truncated.iloc[i], rtol=1e-6)


def test_macd_no_lookahead():
    df = _sample_df()
    i = 150
    full = macd(df["close"], 12, 26, 9)
    truncated = macd(df["close"].iloc[:i + 1], 12, 26, 9)
    assert np.isclose(full["macd_line"].iloc[i], truncated["macd_line"].iloc[i], rtol=1e-9)


def test_atr_no_lookahead():
    df = _sample_df()
    i = 150
    full = atr(df, 14)
    truncated = atr(df.iloc[:i + 1], 14)
    assert np.isclose(full.iloc[i], truncated.iloc[i], rtol=1e-6)


def test_bollinger_no_lookahead():
    df = _sample_df()
    i = 150
    full = bollinger_width(df["close"], 20, 2.0)
    truncated = bollinger_width(df["close"].iloc[:i + 1], 20, 2.0)
    assert np.isclose(full["bb_width"].iloc[i], truncated["bb_width"].iloc[i], rtol=1e-6)


def test_build_all_indicators_no_lookahead():
    """Composite test across the full indicator pipeline at once."""
    df = _sample_df(n=400)
    i = 250
    full = build_all_indicators(df, CFG)
    truncated = build_all_indicators(df.iloc[:i + 1].copy(), CFG)
    check_cols = ["ema_9", "ema_21", "rsi", "macd_line", "atr", "bb_width"]
    for col in check_cols:
        assert np.isclose(full[col].iloc[i], truncated[col].iloc[i], rtol=1e-6, equal_nan=True), f"Leak in {col}"


def test_candle_anatomy_no_lookahead():
    """Row-wise features must be identical regardless of dataset length."""
    df = _sample_df()
    i = 150
    full = candle_anatomy(df)
    truncated = candle_anatomy(df.iloc[:i + 1].copy())
    assert np.isclose(full["body_ratio"].iloc[i], truncated["body_ratio"].iloc[i])
    assert full["candle_direction"].iloc[i] == truncated["candle_direction"].iloc[i]


def test_structure_swing_detection_no_lookahead():
    """
    A swing flag confirmed at row i using the full dataset must ALSO be confirmed
    at row i when only rows [0..i] are available - because by construction the
    confirmation only ever looks lookback candles into the past relative to i.
    """
    df = _sample_df(n=300)
    lookback = 20
    i = 200
    full = detect_structure(df, lookback=lookback)
    truncated = detect_structure(df.iloc[:i + 1].copy(), lookback=lookback)
    assert full["swing_high_confirmed"].iloc[i] == truncated["swing_high_confirmed"].iloc[i]
    assert full["swing_low_confirmed"].iloc[i] == truncated["swing_low_confirmed"].iloc[i]
    assert full["last_structure_high"].iloc[i] == truncated["last_structure_high"].iloc[i] or (
        pd.isna(full["last_structure_high"].iloc[i]) and pd.isna(truncated["last_structure_high"].iloc[i])
    )


def test_mtf_merge_asof_never_uses_future_htf_candle():
    """
    Explicit test that merge_asof backward direction never pulls an HTF candle
    whose timestamp is AFTER the LTF row's timestamp - the classic MTF leak.
    """
    ltf_df = _sample_df(n=100)  # M5
    htf_df = _candles("H1", 20, end_ts=int(ltf_df["timestamp_utc"].max()))
    htf_df["dummy_feature"] = htf_df["close"]

    merged = merge_htf_feature(ltf_df, htf_df, "dummy_feature", "htf_dummy")

    htf_sorted = htf_df.sort_values("timestamp_utc").reset_index(drop=True)
    for idx, row in merged.iterrows():
        if pd.isna(row["htf_dummy"]):
            continue
        matched_htf_ts = htf_sorted.loc[htf_sorted["close"] == row["htf_dummy"], "timestamp_utc"]
        # The matched HTF candle's timestamp must always be <= the LTF row's timestamp
        valid_ts = htf_sorted[htf_sorted["timestamp_utc"] <= row["timestamp_utc"]]
        if len(valid_ts) > 0:
            expected_ts = valid_ts["timestamp_utc"].max()
            actual_htf_row = htf_sorted[htf_sorted["close"] == row["htf_dummy"]]
            if len(actual_htf_row) > 0:
                assert actual_htf_row["timestamp_utc"].iloc[0] <= row["timestamp_utc"], \
                    "Look-ahead detected: merge_asof pulled a future HTF candle!"


def test_mtf_merge_does_not_see_incomplete_htf_bar():
    """AUDIT 2026-08-23 (module 8b): HTF bars are stamped at bar OPEN. An M5
    row at 13:05 sits inside the H1 bar stamped 13:00, which only CLOSES at
    14:00 — its close must never reach that row. Only the previous completed
    H1 bar (12:00) is allowed. The stamp-ordering test above cannot catch this
    (the leaked stamp IS <= the LTF timestamp)."""
    base = int(pd.Timestamp("2026-01-02 12:00", tz="UTC").timestamp())
    ltf_df = pd.DataFrame([
        {"timestamp_utc": base + m * 60, "close": 100.0 + m * 0.1}
        for m in range(0, 120, 5)          # M5 rows 12:00 .. 13:55
    ])
    htf_df = pd.DataFrame([
        {"timestamp_utc": base - 3600, "close": 50.0},   # 11:00 bar — closed
        {"timestamp_utc": base,        "close": 99.0},   # 12:00 bar — closes 13:00
    ])

    merged = merge_htf_feature(ltf_df, htf_df, "close", "htf_close")

    pre_close = merged[merged["timestamp_utc"] < base + 3600]
    assert len(pre_close) > 0
    # Rows before 13:00 may only see the previous completed bar's value.
    assert (pre_close["htf_close"] == 50.0).all(), \
        f"intra-period leak: future H1 close seen pre-13:00: {pre_close['htf_close'].unique()}"


# ---------------------------------------------------------------------------
# N2 (audit 2026-08-10): extend no-look-ahead coverage to the modules the
# original suite skipped (order_flow, smart_money_metrics, fractional_diff,
# regime). Same truncation methodology.
# ---------------------------------------------------------------------------

def test_order_flow_features_no_lookahead():
    """order_flow features at row i must be identical whether computed on the
    full frame or a frame truncated at i (past the CVD anchoring window)."""
    from features.order_flow import add_order_flow_features
    df = _sample_df(n=300)
    df["atr"] = 0.5
    full = add_order_flow_features(df, cvd_window=100)
    i = 200
    trunc = add_order_flow_features(df.iloc[:i + 1].copy(), cvd_window=100)
    for col in ("cvd", "cvd_slope_10", "order_flow_imbalance_14",
                "order_flow_imbalance_50", "vwap", "dist_vwap_atr"):
        assert np.isclose(full[col].iloc[i], trunc[col].iloc[i], rtol=1e-6,
                          equal_nan=True), f"Look-ahead in order_flow column {col}"


# Widest trailing slice read by any helper in features/smart_money_metrics.py
# (calculate_zone_strength uses window=50). Any frame at least this long must
# already contain every bar the metrics are allowed to look at.
SMC_WIDEST_WINDOW = 50


def test_smart_money_metrics_no_lookahead():
    """Institutional metrics must depend ONLY on the trailing bars they read.

    Every calculate_* helper slices df.tail(window) with window <= 50, so the
    returned dict has to be invariant to how much OLDER history is prepended.
    The frame END is therefore pinned while the frame START is varied.

    The previous version of this test moved the end as well (full frame ended
    at bar 300, truncated frame at bar 250), so it compared two completely
    different stretches of market and could not prove anything about
    causality. It passed on synthetic candles by luck and started failing the
    moment it was pointed at real XAUUSD history -- the failure was the test's,
    not the code's.

    Sensitivity: the public API returns QUANTISED scores (1..10 and 5..95), so
    a frame-length dependence smaller than one quantum is invisible here.
    Measured on 60 random windows with injected defects, this form catches
    frame-anchored computations -- the OBV / bb_width / asia-range bug class
    fixed in Layer 0 -- on 93% of windows, and threshold-normalisation drift on
    8%. It is a guard against the former, not a proof against the latter.
    """
    from features.smart_money_metrics import compute_institutional_metrics
    df = _sample_df(n=300)
    df["atr"] = 0.5
    short_len = SMC_WIDEST_WINDOW + 10
    assert len(df) >= short_len + 35, "need enough bars to vary the frame start"

    checked = 0
    for end in range(short_len + 5, len(df) + 1, 30):
        long_frame = df.iloc[:end].copy().reset_index(drop=True)
        short_frame = df.iloc[end - short_len:end].copy().reset_index(drop=True)
        assert len(long_frame) > len(short_frame), "frames must differ in length"
        full = compute_institutional_metrics(long_frame)
        trunc = compute_institutional_metrics(short_frame)
        for key in full:
            assert full[key] == trunc[key], (
                f"Look-ahead in smart-money metric {key!r}: prepending "
                f"{end - short_len} older bars changed it at frame end {end} "
                f"({full[key]!r} != {trunc[key]!r})"
            )
        checked += 1
    assert checked >= 5, f"expected several anchor points, checked {checked}"


def test_fractional_diff_no_lookahead():
    """frac_diff uses fixed weights (independent of data length), so truncation
    invariance holds by construction."""
    from features.fractional_diff import frac_diff
    df = _sample_df(n=300)
    full = frac_diff(df["close"], d=0.5)
    i = 150
    trunc = frac_diff(df["close"].iloc[:i + 1], d=0.5)
    assert np.isclose(full.iloc[i], trunc.iloc[i], rtol=1e-6, equal_nan=True)


def test_regime_classifier_no_lookahead():
    """classify_regime_series at row i must be identical to a truncated run."""
    from features.indicators import build_all_indicators
    from regime.classifier import add_regime_indicators, classify_regime_series
    df = _sample_df(n=300)
    df = build_all_indicators(df, CFG)
    df = add_regime_indicators(df, CFG)
    full = classify_regime_series(df, CFG)
    i = 250
    trunc = classify_regime_series(df.iloc[:i + 1].copy(), CFG)
    assert full.iloc[i] == trunc.iloc[i], "Look-ahead in regime classification"


# ---------------------------------------------------------------------------
# A1-A3 (audit 2026-08-12, "Layer 0"). The suite above never touched the four
# session/day-anchored columns, the OBV level or the bb_width_percentile name
# collision, so all three defects passed CI for months. These tests fail on the
# pre-fix code and pass afterwards.
#
# The legacy _sample_df() helper calls fetch_mock_candles() without end_ts, so
# its window floats with wall-clock time and the session composition of the
# sample changes between runs. Causality tests pin the window instead, so a
# failure is always reproducible.
# ---------------------------------------------------------------------------

FIXED_END_TS = int(pd.Timestamp("2026-08-07T00:00:00Z").timestamp())  # 1786060800


def _sample_df_tf(timeframe: str, n: int, end_ts: int = None, seed: int = 42):
    """Deterministic sample window on an explicit timeframe (never wall-clock).

    Under XAU_TEST_DB this returns the last n REAL candles of that timeframe,
    so the causality proofs below are re-run against broker history. The pinned
    synthetic window is only used when no database is supplied.
    """
    if REAL_DB:
        return _candles(timeframe, n, end_ts=end_ts)
    return _candles(timeframe, n, end_ts=FIXED_END_TS if end_ts is None else end_ts,
                    seed=seed)


def test_asia_session_range_no_lookahead():
    """A1: dist_asia_high_atr / dist_asia_low_atr are live model features
    (model/trainer.FEATURE_COLUMNS). groupby(day).transform("max") broadcast the
    WHOLE day's Asian extreme onto every bar of that day, so a 01:00 UTC bar
    already knew the high the session would print at 07:45."""
    df = _sample_df_tf("M15", n=600)
    full = build_all_indicators(df, CFG)

    day = df["timestamp_utc"] // 86400
    is_asia = df["session"].str.contains("asia", na=False)
    candidates = [
        i for i in range(200, len(df) - 1)
        if is_asia.iloc[i] and bool((is_asia & (day == day.iloc[i])).iloc[i + 1:].any())
    ]
    assert candidates, "sample window contains no mid-Asian-session bar"
    i = candidates[len(candidates) // 2]

    trunc = build_all_indicators(df.iloc[:i + 1].copy(), CFG)
    for col in ("dist_asia_high_atr", "dist_asia_low_atr"):
        assert np.isclose(full[col].iloc[i], trunc[col].iloc[i], rtol=1e-9, equal_nan=True), (
            f"Look-ahead in {col} at row {i}: full={full[col].iloc[i]!r} vs "
            f"truncated={trunc[col].iloc[i]!r} while the Asian session was still open"
        )


def test_asia_session_range_frozen_after_session_close():
    """A1 regression guard: once the Asian session has closed the causal running
    extreme must equal the completed range, so London/NY bars keep the exact
    value they had before the fix (only intra-session bars change)."""
    df = _sample_df_tf("M15", n=600)
    out = build_all_indicators(df, CFG)

    day = df["timestamp_utc"] // 86400
    is_asia = df["session"].str.contains("asia", na=False)
    london = [i for i in range(200, len(df)) if "london" in str(df["session"].iloc[i])]
    assert london, "sample window contains no London bar"
    i = london[len(london) // 2]

    same_day_asia = is_asia & (day == day.iloc[i])
    expected_high = df.loc[same_day_asia, "high"].max()
    expected_low = df.loc[same_day_asia, "low"].min()

    atr_i = out["atr"].iloc[i]
    recovered_high = out["close"].iloc[i] - out["dist_asia_high_atr"].iloc[i] * atr_i
    recovered_low = out["close"].iloc[i] - out["dist_asia_low_atr"].iloc[i] * atr_i
    assert np.isclose(recovered_high, expected_high, rtol=1e-9)
    assert np.isclose(recovered_low, expected_low, rtol=1e-9)


def test_previous_day_levels_are_timeframe_agnostic():
    """A2: pdh/pdl were the current day's completed extreme shifted back by a
    hard-coded 288 BARS - one day only on M5. XAUUSD trades M15 (96 bars/day) so
    dist_pdh_atr silently meant "3 days ago"; EURUSD/GBPUSD trade H1 -> 12 days."""
    for timeframe, n in (("M5", 2000), ("M15", 700), ("H1", 200)):
        df = _sample_df_tf(timeframe, n=n)
        out = build_all_indicators(df, CFG)

        day = df["timestamp_utc"] // 86400
        expected_pdh = day.map(df.groupby(day)["high"].max().shift(1))
        expected_pdl = day.map(df.groupby(day)["low"].min().shift(1))

        atr_col = out["atr"]
        mask = atr_col.notna() & (atr_col > 0) & expected_pdh.notna()
        assert mask.sum() > 50, f"{timeframe}: not enough comparable rows"

        recovered_pdh = out["close"] - out["dist_pdh_atr"] * atr_col
        recovered_pdl = out["close"] - out["dist_pdl_atr"] * atr_col
        assert np.allclose(recovered_pdh[mask], expected_pdh[mask], rtol=1e-9), (
            f"{timeframe}: dist_pdh_atr is not measured from the previous UTC day's high"
        )
        assert np.allclose(recovered_pdl[mask], expected_pdl[mask], rtol=1e-9), (
            f"{timeframe}: dist_pdl_atr is not measured from the previous UTC day's low"
        )


def test_previous_day_levels_no_lookahead_on_m1():
    """A2: M1 is a supported timeframe (data/mt5_provider._TIMEFRAMES). At 1440
    bars/day a 288-bar shift never leaves the CURRENT UTC day, so the completed
    daily extreme is broadcast onto bars that precede it - an outright leak."""
    df = _sample_df_tf("M1", n=1500)
    full = build_all_indicators(df, CFG)
    i = 1200
    trunc = build_all_indicators(df.iloc[:i + 1].copy(), CFG)
    for col in ("dist_pdh_atr", "dist_pdl_atr"):
        assert np.isclose(full[col].iloc[i], trunc[col].iloc[i], rtol=1e-9, equal_nan=True), (
            f"Look-ahead in {col} at row {i}: full={full[col].iloc[i]!r} vs "
            f"truncated={trunc[col].iloc[i]!r}"
        )


def test_obv_is_frame_length_invariant():
    """A2: N1 anchored CVD to a fixed trailing window because an unbounded
    cumsum makes the feature LEVEL depend on where the frame starts. obv() had
    the identical bug: backtest folds start thousands of bars before the live
    300-bar pipeline window, so the same bar got two different values."""
    df = _sample_df_tf("M15", n=600)
    long_frame = build_all_indicators(df, CFG)

    short = df.iloc[300:].copy().reset_index(drop=True)
    short_frame = build_all_indicators(short, CFG)

    i_long, i_short = 599, 299
    assert df["timestamp_utc"].iloc[i_long] == short["timestamp_utc"].iloc[i_short]
    assert np.isclose(
        long_frame["obv"].iloc[i_long], short_frame["obv"].iloc[i_short], rtol=1e-9
    ), (
        "obv depends on the frame start (train/serve skew): "
        f"{long_frame['obv'].iloc[i_long]!r} vs {short_frame['obv'].iloc[i_short]!r}"
    )


def test_bb_width_percentile_has_a_single_owner():
    """A3: build_all_indicators published a 0..1 rolling-100 min-max under the
    SAME name add_regime_indicators writes a 0..100 rolling-50 rank into - and
    that name is a trained model feature. Whichever ran last defined it."""
    from regime.classifier import add_regime_indicators

    df = _sample_df_tf("M15", n=600)
    feat = build_all_indicators(df, CFG)

    assert "bb_width_percentile" not in feat.columns, (
        "features/indicators.py must not publish bb_width_percentile: "
        "regime/classifier.py owns that name (0..100 rank)"
    )
    assert "bb_width_minmax_100" in feat.columns
    minmax = feat["bb_width_minmax_100"]
    assert float(minmax.min()) >= 0.0 and float(minmax.max()) <= 1.0

    with_regime = add_regime_indicators(feat, CFG)
    assert "bb_width_percentile" in with_regime.columns
    pd.testing.assert_series_equal(minmax, with_regime["bb_width_minmax_100"])

    rank = with_regime["bb_width_percentile"].dropna()
    assert float(rank.min()) >= 0.0 and float(rank.max()) <= 100.0
    assert float(rank.max()) > 1.0, (
        "the regime compression threshold (regime.bb_width_compression_pctile=20) "
        "is expressed on a 0..100 scale"
    )


def test_regime_module_rejects_stale_zero_to_one_column():
    """A3 guard: a frame built by a stale features/indicators.py must fail loudly
    instead of being silently overwritten a second time."""
    from regime.classifier import add_regime_indicators

    df = _sample_df_tf("M15", n=300)
    feat = build_all_indicators(df, CFG)
    stale = feat.copy()
    stale["bb_width_percentile"] = stale["bb_width_minmax_100"]  # the pre-fix name clash

    try:
        add_regime_indicators(stale, CFG)
    except ValueError as exc:
        assert "bb_width_minmax_100" in str(exc)
    else:
        raise AssertionError(
            "add_regime_indicators silently overwrote a 0..1 bb_width_percentile"
        )


def test_compression_regime_share_is_sane():
    """A3 guard: if the compression branch ever reads a 0..1 column again, every
    value is below the threshold of 20 and every bar becomes COMPRESSION."""
    from regime.classifier import (
        RegimeLabel,
        add_regime_indicators,
        classify_regime_series,
    )

    df = _sample_df_tf("M15", n=600)
    df = build_all_indicators(df, CFG)
    df = add_regime_indicators(df, CFG)
    labels = classify_regime_series(df, CFG)

    tail = labels.iloc[CFG["regime"]["min_candles_for_regime"]:]
    share = float((tail == RegimeLabel.COMPRESSION).mean())
    assert share < 0.9, (
        f"{share:.0%} of bars classified COMPRESSION - the threshold is being "
        "compared against a 0..1 column"
    )
