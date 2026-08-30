"""The traded-event label must survive model.trainer.build_training_matrix.

Why this file exists
--------------------
A10 added labels for the event the execution engine actually resolves
(protective level before stop). They were never wired into training, and they
could not be wired naively, because two modules disagree about what the integer
0 means in a label column:

    labeling.generate_labels*        0 = neither barrier was hit (no outcome)
    labeling.generate_labels_traded* 0 = the SHORT side is the better bet

build_training_matrix was written for the first convention: in binary mode it
keeps only rows whose label isin([1, -1]) and encodes y = (label == 1). Handing
it the traded label in its {0, 1} form therefore deletes every short row, leaves
a single class, and raises DegenerateLabelSpaceError one layer further in --
which run_backtest catches per fold and degrades to a neutral 0.5, i.e. a clean
backtest with zero trades. Nothing anywhere says "label space mismatch".

So the tests below pin the contract itself rather than any number: the training
path emits -1/+1, the diagnostics keep {0, 1}, and the failure mode of confusing
the two is asserted explicitly so it cannot come back silently.
"""

import numpy as np
import pandas as pd
import pytest

from labeling.label_generator import (
    LABEL_EVENTS,
    TRADED_ENCODINGS,
    generate_labels_atr_scaled,
    generate_labels_from_config,
    generate_labels_traded_direction,
    resolve_label_event,
)
from model.trainer import (
    DegenerateLabelSpaceError,
    build_training_matrix,
    normalize_label_space,
)

ASSET = "TEST"
ATR = 1.0
STEP = 0.5  # price change per bar
LEG = 10  # bars per sawtooth leg -> amplitude 5 ATR
N_BARS = 400
HORIZON = 36


def _sawtooth_df(n: int = N_BARS) -> pd.DataFrame:
    """Deterministic zig-zag where BOTH sides win somewhere.

    Barriers are 1 ATR (protect) and 2 ATR (stop) against a 5 ATR leg, so a bar
    inside a rising leg resolves long-favourable / short-stopped, and a bar just
    past a peak resolves the other way. Bars near the turns resolve the same way
    for both sides and become NaN, which is the real sample's dominant case too.
    """
    closes = []
    price = 100.0
    rising = True
    for i in range(n):
        closes.append(price)
        price = price + STEP if rising else price - STEP
        if (i + 1) % LEG == 0:
            rising = not rising
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "atr": np.full(n, ATR),
            # A real feature column, so build_training_matrix has something to keep.
            "rsi": np.linspace(30.0, 70.0, n),
        }
    )


def _cfg(event: str = None, include_zero_class: bool = False) -> dict:
    cfg = {
        "labeling": {
            "method": "atr_scaled",
            "horizon_candles_n": HORIZON,
            "atr_column": "atr",
            "target_atr_multiplier": 1.2,
            "stop_atr_multiplier": 1.0,
        },
        "signal_grid": {
            "tp1_mult": 1.0,
            "tp2_mult": 1.5,
            "tp3_mult": 2.0,
            "stop_mult": 2.0,
            "breakeven_trigger_atr": 1.0,
        },
        # Zero costs keep the barrier arithmetic exact; the cost filter is
        # exercised by the A10 tests, not here.
        "assets": {ASSET: {"spread_usd": 0.0, "slippage_usd": 0.0}},
        "backtest": {"spread_points": 0, "slippage_points": 0},
        "model": {"include_zero_class": include_zero_class},
    }
    if event is not None:
        cfg["labeling"]["event"] = event
    return cfg


def _traded_labels(df: pd.DataFrame, encoding: str) -> pd.Series:
    return generate_labels_traded_direction(df, _cfg(), ASSET, encoding=encoding)


# ---------------------------------------------------------------------------
# The switch itself
# ---------------------------------------------------------------------------


def test_the_default_config_still_produces_the_old_barrier_label():
    """An absent labeling.event must change nothing at all."""
    df = _sawtooth_df()
    cfg = _cfg()

    assert resolve_label_event(cfg) == "barrier"

    got = generate_labels_from_config(df, cfg)
    expected = generate_labels_atr_scaled(
        df,
        target_atr_multiplier=1.2,
        stop_atr_multiplier=1.0,
        horizon_n=HORIZON,
        atr_col="atr",
    )
    pd.testing.assert_series_equal(got, expected)


def test_an_unknown_label_event_is_rejected():
    df = _sawtooth_df(60)
    with pytest.raises(ValueError, match="Unknown labeling.event"):
        generate_labels_from_config(df, _cfg(event="traded_event"), ASSET)
    assert LABEL_EVENTS == ("barrier", "traded")


def test_the_traded_event_refuses_to_guess_the_asset():
    """Costs and the signal grid are per-asset, so the event is per-asset."""
    df = _sawtooth_df(60)
    with pytest.raises(ValueError, match="asset_key"):
        generate_labels_from_config(df, _cfg(event="traded"))


def test_the_traded_event_refuses_the_three_class_space():
    """0 cannot mean no_trade and short in the same column."""
    df = _sawtooth_df(60)
    with pytest.raises(ValueError, match="include_zero_class"):
        generate_labels_from_config(df, _cfg(event="traded", include_zero_class=True), ASSET)


# ---------------------------------------------------------------------------
# The encoding contract
# ---------------------------------------------------------------------------


def test_traded_event_uses_signal_bar_atr_and_grid_clamp():
    from labeling.label_generator import generate_labels_traded_event

    df = pd.DataFrame(
        {
            "open": [100.0] * 8,
            "high": [100.0, 100.0, 101.1, 100.0, 100.0, 100.0, 100.0, 100.0],
            "low": [100.0] * 8,
            "close": [100.0] * 8,
            "atr": [1.0, 10.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        }
    )
    cfg = _cfg()
    cfg["signal_grid"]["step_max_points"] = 1.0
    labels = generate_labels_traded_event(
        df,
        cfg,
        ASSET,
        direction=1,
        horizon_n=4,
        include_costs=False,
        require_net_positive=False,
    )
    assert labels.iloc[0] == 1.0  # protect=101 from signal ATR; not entry ATR=10


def test_the_wired_training_label_uses_minus_one_for_short():
    df = _sawtooth_df()
    labels = generate_labels_from_config(df, _cfg(event="traded"), ASSET)
    values = set(labels.dropna().unique())

    assert values == {-1.0, 1.0}, (
        "the training label space is {-1: short, +1: long}; a 0 here means the "
        "binary01 diagnostic encoding leaked into the training path"
    )
    assert labels.name == "label"


def test_both_sides_are_present_in_the_sawtooth_fixture():
    """Guards the fixture itself: a one-sided fixture would make the
    class-survival tests below vacuously true."""
    labels = _traded_labels(_sawtooth_df(), "pm1")
    counts = labels.value_counts()
    assert counts.get(1.0, 0) > 20
    assert counts.get(-1.0, 0) > 20


def test_pm1_and_binary01_differ_only_in_the_code_for_short():
    df = _sawtooth_df()
    pm1 = _traded_labels(df, "pm1")
    b01 = _traded_labels(df, "binary01")

    # Same rows are informative under both encodings.
    assert (pm1.isna() == b01.isna()).all()
    # Longs agree; shorts are 0 in one space and -1 in the other.
    assert (pm1[b01 == 1.0] == 1.0).all()
    assert (pm1[b01 == 0.0] == -1.0).all()
    assert (b01 == 0.0).sum() > 0


def test_the_encoding_argument_is_validated():
    df = _sawtooth_df(60)
    with pytest.raises(ValueError, match="encoding"):
        generate_labels_traded_direction(df, _cfg(), ASSET, encoding="pm_one")
    assert TRADED_ENCODINGS == ("binary01", "pm1")


# ---------------------------------------------------------------------------
# The contract with the trainer
# ---------------------------------------------------------------------------


def test_build_training_matrix_keeps_both_sides_of_the_wired_label():
    df = _sawtooth_df()
    cfg = _cfg(event="traded")
    df = df.assign(label=generate_labels_from_config(df, cfg, ASSET))

    X, y, cols = build_training_matrix(df, cfg=cfg)

    assert len(X) == int(df["label"].notna().sum())
    assert set(y.unique()) == {0, 1}, "y must encode {0: short, 1: long}"
    assert (y == 0).sum() > 0 and (y == 1).sum() > 0
    assert "rsi" in cols
    # And the label space is directly trainable: no remap, no refusal.
    assert set(normalize_label_space(y, cfg).unique()) == {0, 1}


def test_the_binary01_space_silently_loses_every_short_row():
    """The failure this PR exists to prevent, asserted rather than described.

    build_training_matrix keeps isin([1, -1]), so under the {0: short, 1: long}
    encoding every short row is dropped as "no outcome". Nothing raises here;
    the run only dies later, per fold, as a no-signal warning.
    """
    df = _sawtooth_df()
    cfg = _cfg()
    b01 = _traded_labels(df, "binary01")
    shorts = int((b01 == 0.0).sum())
    longs = int((b01 == 1.0).sum())
    assert shorts > 0 and longs > 0

    X, y, _ = build_training_matrix(df.assign(label=b01), cfg=cfg)

    assert len(X) == longs, "every short row was dropped, silently"
    assert set(y.unique()) == {1}
    # ...and only now does anything complain.
    with pytest.raises(DegenerateLabelSpaceError, match="single class"):
        normalize_label_space(y, cfg)


def test_flipping_the_switch_actually_changes_the_training_target():
    """Both label spaces are defined on the same bars, and they disagree.

    If they agreed almost everywhere, the wiring would be cosmetic and the
    retraining experiment pointless.
    """
    df = _sawtooth_df()
    barrier = generate_labels_from_config(df, _cfg())
    traded = generate_labels_from_config(df, _cfg(event="traded"), ASSET)

    both = barrier.notna() & traded.notna()
    assert both.sum() > 50
    directional = both & barrier.isin([1.0, -1.0])
    disagreement = float((barrier[directional] != traded[directional]).mean())
    assert disagreement > 0.05, (
        "the traded event and the triple barrier should not describe the same outcome on the same bars"
    )
