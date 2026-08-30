"""Purge/embargo behaviour and slicing parity for the walk-forward harness.

History (2026-08-14): `scripts/run_backtest.py` and `scripts/deflated_sharpe.py`
each sliced their own train/test frames. Only the former purged the train rows
whose triple-barrier labels resolve inside the test window, so on the same
XAUUSD sample the reported backtest and the decision gate disagreed by 6x
(365 trades / -396.55 versus 318 trades / -2415.20). `split_fold_frames` is now
the single slicing path; these tests fail the moment someone re-derives the
boundaries by hand again.

Boundary convention, since it is easy to get wrong (it was, in the first version
of this file): the purge cutoff is INCLUSIVE. A bar standing exactly
`horizon + embargo` bars before the test window survives, because its label
resolves `embargo` bars before the first test bar and therefore leaks nothing.
So the *distance* is `horizon + embargo` bars while the number of *dropped rows*
is `horizon + embargo - 1`. Assert the invariant, then derive the count from it.

Deliberately dependency-light: pandas + numpy only, no model stack, so this runs
anywhere pytest runs.
"""

import pandas as pd

from backtest.walk_forward import (
    bar_seconds,
    generate_windows,
    purge_train_frame,
    run_walk_forward,
    split_fold_frames,
)

BAR_SECS = 900  # M15
START_TS = 1_700_000_000
TRAIN_DAYS = 4
TEST_DAYS = 2
STEP_DAYS = 2
HORIZON = 36
EMBARGO = 100


def _df(n_days: int = 12) -> pd.DataFrame:
    """Gapless M15 grid: with no weekend holes the expected purge distance is an
    exact bar count, which is what makes the assertions below sharp."""
    n = n_days * 96
    return pd.DataFrame(
        {
            "timestamp_utc": [START_TS + i * BAR_SECS for i in range(n)],
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
        }
    )


def _cfg(horizon: int = HORIZON, embargo: int = EMBARGO) -> dict:
    return {
        "labeling": {"horizon_candles_n": horizon},
        "backtest": {
            "walk_forward": {
                "train_window_days": TRAIN_DAYS,
                "test_window_days": TEST_DAYS,
                "step_days": STEP_DAYS,
                "embargo_candles": embargo,
            }
        },
    }


def _raw_train(df: pd.DataFrame, w) -> pd.DataFrame:
    return df[(df["timestamp_utc"] >= w.train_start_ts) & (df["timestamp_utc"] < w.train_end_ts)]


def _dropped(horizon: int = HORIZON, embargo: int = EMBARGO) -> int:
    """Rows removed by an inclusive cutoff placed `horizon + embargo` bars back."""
    return horizon + embargo - 1


def test_bar_seconds_detects_the_sampling_grid():
    assert bar_seconds(_df()) == BAR_SECS


def test_bar_seconds_survives_a_degenerate_frame():
    assert bar_seconds(pd.DataFrame({"timestamp_utc": [START_TS]})) == 1
    assert bar_seconds(pd.DataFrame({"close": [1.0, 2.0]})) == 1


def test_last_surviving_label_ends_one_full_embargo_before_the_test_window():
    """The invariant the purge exists for. Everything else is arithmetic."""
    df = _df()
    cfg = _cfg()
    windows = generate_windows(df, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    assert windows, "fixture must produce at least one window"
    for w in windows:
        purged = purge_train_frame(_raw_train(df, w), w.test_start_ts, cfg, BAR_SECS)
        last_kept = int(purged["timestamp_utc"].max())
        label_end = last_kept + HORIZON * BAR_SECS
        assert (w.test_start_ts - label_end) == EMBARGO * BAR_SECS
        assert (w.test_start_ts - last_kept) == (HORIZON + EMBARGO) * BAR_SECS


def test_purge_drops_horizon_plus_embargo_minus_one_rows():
    df = _df()
    cfg = _cfg()
    w = generate_windows(df, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)[0]
    raw = _raw_train(df, w)
    purged = purge_train_frame(raw, w.test_start_ts, cfg, BAR_SECS)
    assert len(purged) == len(raw) - _dropped()


def test_embargo_zero_purges_only_the_label_horizon():
    """With no embargo the last surviving label ends exactly ON the first test bar,
    which is the 1-bar touch the embargo exists to remove - hence embargo: 100 in
    config.yaml. Pinned so the degenerate case stays visible."""
    df = _df()
    cfg = _cfg(embargo=0)
    w = generate_windows(df, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)[0]
    raw = _raw_train(df, w)
    purged = purge_train_frame(raw, w.test_start_ts, cfg, BAR_SECS)
    last_kept = int(purged["timestamp_utc"].max())
    assert last_kept + HORIZON * BAR_SECS == w.test_start_ts
    assert len(purged) == len(raw) - _dropped(embargo=0)


def test_zero_horizon_disables_the_purge():
    df = _df()
    cfg = _cfg(horizon=0)
    w = generate_windows(df, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)[0]
    raw = _raw_train(df, w)
    assert len(purge_train_frame(raw, w.test_start_ts, cfg, BAR_SECS)) == len(raw)


def test_purged_train_never_overlaps_the_test_window():
    df = _df()
    cfg = _cfg()
    for w in generate_windows(df, TRAIN_DAYS, TEST_DAYS, STEP_DAYS):
        train_df, test_df = split_fold_frames(df, cfg, w)
        assert len(train_df) > 0 and len(test_df) > 0
        assert int(train_df["timestamp_utc"].max()) < int(test_df["timestamp_utc"].min())


def test_split_fold_frames_reindexes_train_positionally():
    """Uniqueness sample weights are keyed by position, so a train frame that
    kept the parent frame's index would silently mis-align them."""
    df = _df()
    cfg = _cfg()
    w = generate_windows(df, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)[1]
    train_df, _ = split_fold_frames(df, cfg, w)
    assert list(train_df.index) == list(range(len(train_df)))


def test_split_fold_frames_is_exactly_what_run_walk_forward_passes():
    """Anti-drift pin: any harness that wants the same folds must call
    split_fold_frames instead of re-deriving the boundaries."""
    df = _df()
    cfg = _cfg()
    seen = []

    def spy(train_df, test_df, cfg_inner):
        seen.append(
            (
                len(train_df),
                int(train_df["timestamp_utc"].max()),
                len(test_df),
                int(test_df["timestamp_utc"].min()),
                int(test_df["timestamp_utc"].max()),
            )
        )
        return {}

    results = run_walk_forward(df, cfg, spy)
    windows = generate_windows(df, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    assert len(seen) == len(results) == len(windows)

    secs = bar_seconds(df)
    for w, observed in zip(windows, seen):
        train_df, test_df = split_fold_frames(df, cfg, w, bar_secs=secs)
        assert observed == (
            len(train_df),
            int(train_df["timestamp_utc"].max()),
            len(test_df),
            int(test_df["timestamp_utc"].min()),
            int(test_df["timestamp_utc"].max()),
        )


def test_reported_purged_train_rows_is_the_purged_count():
    """`purged_train_rows` lands in logs/backtest_<asset>.csv and is the only
    published evidence that the purge ran; it must not report the raw window."""
    df = _df()
    cfg = _cfg()
    results = run_walk_forward(df, cfg, lambda tr, te, c: {})
    windows = generate_windows(df, TRAIN_DAYS, TEST_DAYS, STEP_DAYS)
    for w, r in zip(windows, results):
        raw = _raw_train(df, w)
        assert r["purged_train_rows"] == len(raw) - _dropped()
        assert r["purged_train_rows"] < len(raw)
