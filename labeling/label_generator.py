"""
Label generator for supervised learning.

Supports:
- fixed barriers: absolute target/stop distances
- atr_scaled barriers: target/stop distances derived from the row's ATR
- traded_event barriers: the event the execution engine actually resolves
  (protective level before stop), see generate_labels_traded_event

WHICH EVENT IS TRAINED ON (labeling.event)
------------------------------------------
generate_labels_from_config dispatches on `labeling.event`:

  "barrier" (default)  the historical triple barrier, via labeling.method
                       (fixed / atr_scaled). This is what every model in the
                       project was trained on up to now.
  "traded"             generate_labels_traded_direction, i.e. the event the
                       execution engine resolves. Requires asset_key, because
                       the event is defined by the per-asset signal grid and
                       execution costs.

The default keeps existing behaviour bit-for-bit, so flipping the switch is an
explicit, per-asset decision (assets.<KEY>.labeling.event) rather than something
a config refactor can do by accident.

CRITICAL NO-LOOK-AHEAD WARNING:
This module is intentionally forward-looking for OFFLINE labeling only.
"""

import numpy as np
import pandas as pd

# Values accepted by labeling.event (see generate_labels_from_config).
LABEL_EVENTS = ("barrier", "traded")

# Label spaces the traded direction label can be emitted in. See
# generate_labels_traded_direction for why both exist.
TRADED_ENCODINGS = ("binary01", "pm1")

# P2-41 / TZ 9.x: version of the labeling OUTPUT format produced by this
# module. Any change to the label semantics (barrier rules, traded-event
# resolution, encoding mapping, same-candle ambiguity policy) MUST bump this
# constant and be recorded in docs/MIGRATIONS.md — labels trained against one
# version must never be mixed silently with another. The label Series carries
# no per-row version column (labels are offline artifacts keyed by bar
# timestamp + asset), so callers who persist labeled datasets should store
# this value alongside them (e.g. in the training metadata bundle).
LABELING_SCHEMA_VERSION = "labels.v1"


def resolve_label_event(cfg: dict) -> str:
    """Return the configured labeling.event, validated.

    Public so callers can log WHICH event a run was labelled with. A run whose
    label space is not printed anywhere is a run whose numbers cannot be
    attributed later.
    """
    lab_cfg = (cfg or {}).get("labeling", {}) or {}
    event = str(lab_cfg.get("event", "barrier")).strip().lower()
    if event not in LABEL_EVENTS:
        raise ValueError(f"Unknown labeling.event: {event!r}; expected one of {LABEL_EVENTS}")
    return event


def generate_labels(
    df: pd.DataFrame, target_x: float, stop_y: float, horizon_n: int, price_col: str = "close"
) -> pd.Series:
    n = len(df)
    highs = df["high"].values
    lows = df["low"].values
    entry_prices = df[price_col].values

    labels = np.full(n, np.nan)

    for i in range(n):
        if i + horizon_n >= n:
            continue

        entry = entry_prices[i]
        upper_barrier = entry + target_x
        lower_barrier = entry - stop_y

        outcome = 0
        for j in range(i + 1, i + horizon_n + 1):
            hit_upper = highs[j] >= upper_barrier
            hit_lower = lows[j] <= lower_barrier
            if hit_upper and hit_lower:
                # Same-candle touch of BOTH barriers: with OHLC-only data the
                # intrabar order is unknowable (no tick replay). Labeling it
                # either direction is a SYSTEMATIC bias — this code used to
                # always emit -1 (short), silently skewing training labels
                # toward the lower barrier. Per the quant audit: ambiguous
                # observations are excluded (NaN) instead.
                outcome = np.nan
                break
            elif hit_upper:
                outcome = 1
                break
            elif hit_lower:
                outcome = -1
                break

        labels[i] = outcome

    return pd.Series(labels, index=df.index, name="label")


def generate_labels_atr_scaled(
    df: pd.DataFrame,
    target_atr_multiplier: float,
    stop_atr_multiplier: float,
    horizon_n: int,
    price_col: str = "close",
    atr_col: str = "atr",
) -> pd.Series:
    """
    Triple-barrier labels with per-row barrier widths scaled by ATR.
    Rows with missing/nonpositive ATR are left as NaN.
    """
    n = len(df)
    highs = df["high"].values
    lows = df["low"].values
    entry_prices = df[price_col].values
    atr_values = df[atr_col].values

    labels = np.full(n, np.nan)

    for i in range(n):
        if i + horizon_n >= n:
            continue

        atr_i = atr_values[i]
        if pd.isna(atr_i) or atr_i <= 0:
            continue

        entry = entry_prices[i]
        upper_barrier = entry + atr_i * target_atr_multiplier
        lower_barrier = entry - atr_i * stop_atr_multiplier

        outcome = 0
        for j in range(i + 1, i + horizon_n + 1):
            hit_upper = highs[j] >= upper_barrier
            hit_lower = lows[j] <= lower_barrier
            if hit_upper and hit_lower:
                # Same-candle touch of BOTH barriers: with OHLC-only data the
                # intrabar order is unknowable (no tick replay). Labeling it
                # either direction is a SYSTEMATIC bias — this code used to
                # always emit -1 (short), silently skewing training labels
                # toward the lower barrier. Per the quant audit: ambiguous
                # observations are excluded (NaN) instead.
                outcome = np.nan
                break
            elif hit_upper:
                outcome = 1
                break
            elif hit_lower:
                outcome = -1
                break

        labels[i] = outcome

    return pd.Series(labels, index=df.index, name="label")


# ---------------------------------------------------------------------------
# A10: labels for the event the execution engine actually resolves.
# ---------------------------------------------------------------------------


def _execution_costs(cfg: dict, asset_key: str) -> tuple:
    """(spread, slippage) in absolute price units, resolved exactly like
    EnsembleBacktester.__init__ does: per-asset spread_usd / slippage_usd with a
    fallback to the global *_points values divided by 100."""
    bt_cfg = cfg.get("backtest", {})
    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    spread = asset_cfg.get("spread_usd", bt_cfg.get("spread_points", 25) / 100.0)
    slippage = asset_cfg.get("slippage_usd", bt_cfg.get("slippage_points", 5) / 100.0)
    return float(spread), float(slippage)


def generate_labels_traded_event(
    df: pd.DataFrame,
    cfg: dict,
    asset_key: str = "XAUUSD",
    direction: int = 1,
    horizon_n: int = None,
    include_costs: bool = True,
    require_net_positive: bool = True,
    use_regime_overrides: bool = True,
) -> pd.Series:
    """Binary label for the event EnsembleBacktester actually resolves.

    WHY THIS EXISTS (A10)
    ---------------------
    The training label and the traded trade were describing different events.
    Labels came from the triple barrier (target 1.2 ATR, stop 1.0 ATR, horizon
    36). The trade resolves against the signal grid: TP1 at 1.0 ATR banks 50%
    and moves the stop to entry, TP3 at 2.0 ATR runs the remainder, and the
    stop sits at 2.0 ATR. Measured over the 12 pre-lock folds, 80.7% of exits
    were post-TP1 breakeven scratches, 15.9% were TP3 runners and only 3.2%
    were full stops -- so the old label described the outcome of 3.2% of
    trades. A model trained on it cannot express a preference about the trade
    that is actually placed.

    THE EVENT
    ---------
    label = 1  the protective level is touched before the stop. Once that
               happens the stop moves to entry, so the trade can no longer take
               the full 2x loss: it is either a scratch or a runner.
    label = 0  the stop is touched first -> full loss.
    label = NaN neither barrier resolves within the horizon (timeout), ATR is
               missing/nonpositive, or the event is not tradable net of costs.

    The protective level is breakeven_trigger_atr * (TP1 distance), which is the
    level the engine uses to move the stop to entry. With the legacy
    breakeven_trigger_atr = 1.0 it coincides with TP1; assets configured with an
    early breakeven (e.g. FX at 0.5) get their own, nearer level.

    FIDELITY TO THE ENGINE
    ----------------------
    - Alignment: the label is stored on the SIGNAL bar, while the entry is the
      NEXT bar's open. This matches fill_mode="next_open", so a model consuming
      causal features at the signal bar is answering the question that will
      actually be asked of it.
    - Entry price: the next bar's open plus half-spread plus one slippage, both
      adverse, exactly as _apply_slippage and the entry block do.
    - Barrier widths: the causal ATR of the SIGNAL bar, resolved through the
      same fixed-step/clamp policy as live inference, then multiplied once by
      tp1/stop mult. The entry bar's completed ATR is not known at its open.
    - Grid resolution: get_signal_grid with the per-asset section, and when
      use_regime_overrides is set, the regime of the SIGNAL bar, which is the
      regime the engine passes (regimes[i - 1] at entry bar i).
    - Same-bar double touch resolves to the stop, mirroring the engine's
      conservative double_touch_stop rule. Barrier crossing is tested against
      raw highs/lows because exit costs change the money booked, not which
      level is reached first.
    - Exits are scanned from the bar after entry, since the engine never
      evaluates barriers on the entry bar itself.

    COSTS (A12)
    -----------
    require_net_positive drops events whose TP1 distance cannot cover the
    round-trip cost (a full spread plus two slippages). Reaching a target that
    does not pay for its own execution is not a favourable outcome, and
    labelling it 1 is how a backtest quietly manufactures edge on tight-range
    assets. On gold the cost is ~0.35 against an ATR of several dollars, so this
    is a no-op there and a real filter on FX.

    Returns a Series named "label_traded" aligned to df.index.
    """
    from config.loader import get_signal_grid, resolve_signal_step

    if int(direction) not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction!r}")
    dir_ = int(direction)

    lab_cfg = cfg.get("labeling", {})
    asset_cfg = cfg.get("assets", {}).get(asset_key, {})
    atr_col = lab_cfg.get("atr_column", "atr")
    if atr_col not in df.columns:
        raise ValueError(f"ATR column {atr_col!r} missing from frame")
    horizon = int(horizon_n if horizon_n is not None else lab_cfg.get("horizon_candles_n", 36))
    spread, slippage = _execution_costs(cfg, asset_key)
    round_trip = spread + 2.0 * slippage

    base_grid = get_signal_grid(cfg, asset_cfg)
    grid_cache = {None: base_grid}

    def _grid_for(regime_name):
        if not use_regime_overrides or regime_name is None:
            return base_grid
        if regime_name not in grid_cache:
            grid_cache[regime_name] = get_signal_grid(cfg, asset_cfg, regime=regime_name)
        return grid_cache[regime_name]

    n = len(df)
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    atrs = df[atr_col].values
    has_regime = use_regime_overrides and ("regime" in df.columns)
    regimes = df["regime"].values if has_regime else None

    labels = np.full(n, np.nan)

    for s in range(n - 1):
        entry_bar = s + 1
        if entry_bar + 1 >= n:
            break

        atr_signal = atrs[s]
        if pd.isna(atr_signal) or atr_signal <= 0:
            continue
        atr_signal = float(atr_signal)

        if has_regime:
            reg = regimes[s]
            reg_name = reg.value if hasattr(reg, "value") else str(reg)
        else:
            reg_name = None
        grid = _grid_for(reg_name)
        tp1_mult = float(grid.get("tp1_mult", 1.0))
        stop_mult = float(grid.get("stop_mult", 3.0))
        be_trigger = float(grid.get("breakeven_trigger_atr", 1.0))
        step = resolve_signal_step(atr_signal, grid)

        tp1_distance = step * tp1_mult
        if require_net_positive and tp1_distance <= round_trip:
            # The first target cannot pay for its own execution.
            continue

        entry = float(opens[entry_bar])
        if include_costs:
            entry = entry + dir_ * (spread / 2.0) + dir_ * slippage

        protect_level = entry + dir_ * be_trigger * tp1_distance
        stop_level = entry - dir_ * step * stop_mult

        last_bar = min(n - 1, entry_bar + horizon)
        outcome = np.nan
        for j in range(entry_bar + 1, last_bar + 1):
            if dir_ == 1:
                hit_protect = highs[j] >= protect_level
                hit_stop = lows[j] <= stop_level
            else:
                hit_protect = lows[j] <= protect_level
                hit_stop = highs[j] >= stop_level

            if hit_stop and hit_protect:
                # Intrabar order unknowable -> assume the stop, like the engine.
                outcome = 0.0
                break
            if hit_protect:
                outcome = 1.0
                break
            if hit_stop:
                outcome = 0.0
                break

        labels[s] = outcome

    return pd.Series(labels, index=df.index, name="label_traded")


def generate_labels_traded_direction(
    df: pd.DataFrame,
    cfg: dict,
    asset_key: str = "XAUUSD",
    encoding: str = "binary01",
    **kwargs,
) -> pd.Series:
    """Which SIDE is the better bet under the real trade geometry.

    Evaluates generate_labels_traded_event for both directions and keeps only
    bars where the two sides disagree:

        long wins    long reaches its protective level first, short does not
        short wins   short reaches its protective level first, long does not
        label = NaN  both sides resolve the same way (or either is unresolved)

    The NaN share is the headline diagnostic. The protective level sits at 1x
    ATR while the stop sits at 2x ATR, so both directions usually resolve
    favourably within 36 bars; every such bar carries no information about which
    side to take. If NaN dominates, no direction model trained on this geometry
    can work, and the fix is the geometry (or a meta-label on trade quality),
    not a better classifier.

    ENCODING (why there are two)
    ----------------------------
    "binary01"  {0: short, 1: long}. What ModelPredictor's binary branch
                decodes on OUTPUT (p_short = P(class 0), p_long = P(class 1)),
                and what the A10 diagnostics (diag_traded_event.py,
                traded_event_summary) count.
    "pm1"       {-1: short, +1: long}. What model.trainer.build_training_matrix
                accepts on INPUT: in binary mode it keeps only rows whose label
                isin([1, -1]) and then encodes y = (label == 1). Feeding it the
                "binary01" space silently DROPS every short row, because 0 means
                "no barrier hit" in the triple-barrier space that filter was
                written for. The surviving single class then raises
                DegenerateLabelSpaceError, and a walk-forward run degrades every
                fold to a neutral 0.5 -- i.e. an empty backtest that reads as
                "the traded label does not work" instead of "the label space was
                mismatched". Any code path that trains on this label must use
                "pm1"; generate_labels_from_config does.

    Emitted with name "label" in both encodings.
    """
    if encoding not in TRADED_ENCODINGS:
        raise ValueError(f"encoding must be one of {TRADED_ENCODINGS}, got {encoding!r}")

    long_lab = generate_labels_traded_event(df, cfg, asset_key, direction=1, **kwargs).values
    short_lab = generate_labels_traded_event(df, cfg, asset_key, direction=-1, **kwargs).values

    short_code = -1.0 if encoding == "pm1" else 0.0

    out = np.full(len(df), np.nan)
    resolved = (~np.isnan(long_lab)) & (~np.isnan(short_lab))
    out[resolved & (long_lab == 1.0) & (short_lab == 0.0)] = 1.0
    out[resolved & (long_lab == 0.0) & (short_lab == 1.0)] = short_code
    return pd.Series(out, index=df.index, name="label")


def traded_event_summary(
    df: pd.DataFrame,
    cfg: dict,
    asset_key: str = "XAUUSD",
    **kwargs,
) -> dict:
    """Base rates of the traded event, per side and for the direction choice.

    Interpretation guide:
    - `long_favourable_pct` / `short_favourable_pct` are the unconditional
      probabilities of reaching the protective level before the stop. The live
      backtest achieved 96.8% on its 472 selected entries. If these
      unconditional rates are also ~97%, the entry selection contributed
      nothing and the result is pure barrier geometry. If they are near the
      driftless random-walk value of stop/(protect+stop), the selection was
      doing something.
    - `direction_defined_pct` is how often the two sides disagree, i.e. the
      fraction of the sample that carries any directional information at all.
    - `direction_long_share_pct` is the class balance of that subset. Because
      the ensemble compares p against absolute constants (0.55 / 0.62 / 0.71),
      any deviation from 50% here becomes a permanent directional bias in
      production.
    """
    long_lab = generate_labels_traded_event(df, cfg, asset_key, direction=1, **kwargs)
    short_lab = generate_labels_traded_event(df, cfg, asset_key, direction=-1, **kwargs)
    dir_lab = generate_labels_traded_direction(df, cfg, asset_key, **kwargs)

    def _side(lab):
        valid = lab.dropna()
        total = len(valid)
        return {
            "resolved": total,
            "unresolved": int(lab.isna().sum()),
            "favourable_pct": float((valid == 1.0).sum() / total * 100) if total else float("nan"),
        }

    long_s = _side(long_lab)
    short_s = _side(short_lab)
    dir_valid = dir_lab.dropna()

    return {
        "rows": int(len(df)),
        "long_resolved": long_s["resolved"],
        "long_unresolved": long_s["unresolved"],
        "long_favourable_pct": long_s["favourable_pct"],
        "short_resolved": short_s["resolved"],
        "short_unresolved": short_s["unresolved"],
        "short_favourable_pct": short_s["favourable_pct"],
        "direction_defined": int(len(dir_valid)),
        "direction_defined_pct": float(len(dir_valid) / len(df) * 100) if len(df) else float("nan"),
        "direction_long_share_pct": (
            float((dir_valid == 1.0).sum() / len(dir_valid) * 100) if len(dir_valid) else float("nan")
        ),
    }


def generate_labels_from_config(df: pd.DataFrame, cfg: dict, asset_key: str = None) -> pd.Series:
    """The single entry point every training path uses to build `label`.

    Dispatches on labeling.event (see module docstring and resolve_label_event).
    The switch lives HERE rather than at each call site on purpose: run_backtest
    and scripts/deflated_sharpe.py share this function, and a switch duplicated
    per stand is exactly how the two stands came to disagree about purging.
    """
    lab_cfg = cfg["labeling"]
    event = resolve_label_event(cfg)

    if event == "traded":
        if asset_key is None:
            raise ValueError(
                "labeling.event='traded' requires asset_key: the traded event is "
                "defined by the per-asset execution costs (spread_usd / "
                "slippage_usd) and signal grid, so labelling with another "
                "asset's geometry would silently mislabel the sample. Call "
                "generate_labels_from_config(df, cfg, asset_key=<KEY>)."
            )
        if bool((cfg.get("model") or {}).get("include_zero_class", False)):
            raise ValueError(
                "labeling.event='traded' is incompatible with "
                "model.include_zero_class=true: in the three-class space 0 means "
                "no_trade, while the traded direction label uses -1/+1 for the "
                "two sides and NaN for 'both sides resolve the same way'. There "
                "is no no_trade class to learn under this event -- set "
                "include_zero_class=false for this asset, or keep "
                "labeling.event='barrier'."
            )
        # "pm1" is mandatory here: build_training_matrix filters on
        # isin([1, -1]), so the {0, 1} space would lose every short row.
        return generate_labels_traded_direction(df, cfg, asset_key, encoding="pm1")

    method = lab_cfg.get("method", "fixed")

    if method == "fixed":
        return generate_labels(
            df,
            target_x=lab_cfg["target_pips_x"],
            stop_y=lab_cfg["stop_pips_y"],
            horizon_n=lab_cfg["horizon_candles_n"],
        )

    if method == "atr_scaled":
        return generate_labels_atr_scaled(
            df,
            target_atr_multiplier=lab_cfg["target_atr_multiplier"],
            stop_atr_multiplier=lab_cfg["stop_atr_multiplier"],
            horizon_n=lab_cfg["horizon_candles_n"],
            atr_col=lab_cfg.get("atr_column", "atr"),
        )

    raise ValueError(f"Unknown labeling.method: {method}")


def label_distribution_summary(labels: pd.Series) -> dict:
    valid = labels.dropna()
    total = len(valid)
    if total == 0:
        return {"total_valid": 0}
    return {
        "total_valid": total,
        "pct_upper_hit": float((valid == 1).sum() / total * 100),
        "pct_lower_hit": float((valid == -1).sum() / total * 100),
        "pct_no_hit": float((valid == 0).sum() / total * 100),
        "nan_count": int(labels.isna().sum()),
    }
