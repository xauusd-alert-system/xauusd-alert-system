"""run_book_ensemble_backtest - real-data backtest of the book ensemble signal.

Closes the loop that the report's section 3 numbers only imply: do the
trained book-protocol models, run through the SAME decision chain as the
live producer (T-16 ``publish_book_signals``), produce a tradable track
record on out-of-sample real data?

Decision chain (identical thresholds/geometry to the producer):

    features (train normalization params, verbatim inference path)
      -> FC + LSTM + MHA forward -> model_probability
      -> ensemble_vote (TradeLevel 0.6, min_agreement 0.6)   [T-25]
      -> trade with ATR SL/TP (1.5 x ATR stop, 2:1 RR)       [T-16 geometry]
      -> forward_metrics + evaluate_model_acceptance          [T-03]
      -> score_from_trades (PF*sqrt(trades) - w*DD criterion) [T-08]

Honesty rules:
* only the TEST slice of the same chronological 60/20/20 split used by the
  training run is traded (``--max-bars`` must match the training command);
* entry is the NEXT bar's open after the signal bar (no look-ahead), minus
  half the spread; exit pays the other half;
* if a bar touches both SL and TP, the SL is assumed to fill first
  (pessimistic ordering);
* one position at a time - signals fired while a trade is open are counted
  as ``rejected_while_busy`` (mirrors the EA);
* the acceptance verdict is computed by the production gate, not by hand:
  a model with no edge is expected to FAIL it, and that is the deliverable.

Usage::

    python -m scripts.run_book_ensemble_backtest \
        --models-dir output/book_experiments_real \
        --db data/market_data_external.sqlite --asset XAUUSD \
        --timeframe M15 --max-bars 80000 \
        --out output/book_ensemble_backtest/report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backtest.tester_criterion import score_from_trades  # noqa: E402
from backtest.validation_protocol import (  # noqa: E402
    evaluate_model_acceptance,
    forward_metrics,
)
from model.book_nn.ensemble_vote import ensemble_vote, model_probability  # noqa: E402
from model.sample_generator import (  # noqa: E402
    DEFAULT_CFG,
    FEATURE_COLUMNS_BASE,
    FEATURE_COLUMNS_EXTENDED,
    apply_normalization,
    build_book_features,
    build_multi_horizon_target,
    load_normalization_params,
    make_windowed_samples,
    split_indices_time_ordered,
)
from scripts.publish_book_signals import load_ensemble  # noqa: E402
from scripts.run_book_experiments import load_candles  # noqa: E402

logger = logging.getLogger("run_book_ensemble_backtest")


def _atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    """Causal ATR per bar (same TR definition as sample_generator/publisher)."""
    high, low, close = (df[c].astype(float) for c in ("high", "low", "close"))
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _simulate_trade(df: pd.DataFrame, sig_bar: int, direction: int,
                    atr: float, horizon: int, atr_mult: float,
                    risk_reward: float, spread: float) -> dict | None:
    """One position from the bar after the signal; returns the trade record.

    Entry: open of ``sig_bar + 1`` (no look-ahead), paying half the spread.
    Exit: ATR stop / take-profit barrier, pessimistic SL-first ordering on
    ambiguous bars, else time exit at the horizon bar's close. The other
    half of the spread is paid on exit.
    """
    entry_bar = sig_bar + 1
    if entry_bar >= len(df) or not np.isfinite(atr) or atr <= 0:
        return None
    half = spread / 2.0
    # barriers are placed off the MID open price - the same convention as
    # the producer (SL/TP levels from the reference price, fills pay the
    # half-spread) - so the round-trip friction is exactly one spread
    mid_entry = float(df["open"].iloc[entry_bar])
    entry = mid_entry + direction * half
    stop_dist = atr * atr_mult
    if direction > 0:
        sl, tp = mid_entry - stop_dist, mid_entry + stop_dist * risk_reward
    else:
        sl, tp = mid_entry + stop_dist, mid_entry - stop_dist * risk_reward

    last_bar = min(entry_bar + horizon - 1, len(df) - 1)
    exit_price, exit_reason, exit_bar = None, None, last_bar
    for j in range(entry_bar, last_bar + 1):
        hi = float(df["high"].iloc[j])
        lo = float(df["low"].iloc[j])
        if direction > 0:
            if lo <= sl:                       # pessimistic: stop first
                exit_price, exit_reason, exit_bar = sl, "sl", j
                break
            if hi >= tp:
                exit_price, exit_reason, exit_bar = tp, "tp", j
                break
        else:
            if hi >= sl:
                exit_price, exit_reason, exit_bar = sl, "sl", j
                break
            if lo <= tp:
                exit_price, exit_reason, exit_bar = tp, "tp", j
                break
    if exit_price is None:                     # time exit at horizon
        exit_price, exit_reason = float(df["close"].iloc[last_bar]), "time"
    exit_price -= direction * half             # pay the other half-spread

    pnl = (exit_price - entry) * direction     # price units, per 1 unit
    return {
        "signal_bar": int(sig_bar),
        "entry_bar": int(entry_bar),
        "exit_bar": int(exit_bar),
        "bars_held": int(exit_bar - entry_bar + 1),
        "direction": int(direction),
        "entry": round(entry, 3),
        "exit": round(exit_price, 3),
        "sl": round(sl, 3),
        "tp": round(tp, 3),
        "atr": round(float(atr), 3),
        "exit_reason": exit_reason,
        "pnl": round(float(pnl), 3),
    }


def run_ensemble_backtest(models_dir: str, db: str, asset: str,
                          timeframe: str,
                          window: int = DEFAULT_CFG["window"],
                          horizons: tuple[int, ...] = (6, 12),
                          horizon_bars: int = 12,
                          trade_level: float = 0.6,
                          min_agreement: float = 0.6,
                          atr_mult: float = 1.5,
                          risk_reward: float = 2.0,
                          spread: float = 0.30,
                          initial_balance: float = 100.0,
                          max_bars: int | None = 80000) -> dict:
    """Backtest the trained ensemble on the test slice of the candle store."""
    norm_params = load_normalization_params(
        os.path.join(models_dir, "normalization_params.json"))
    nets = load_ensemble(models_dir)

    df = load_candles(asset, timeframe, db)
    if df is None:
        raise ValueError(f"no {asset} {timeframe} candles in {db}")
    if max_bars and len(df) > max_bars:
        df = df.tail(int(max_bars)).reset_index(drop=True)
    df = df.reset_index(drop=True)

    cfg = {**DEFAULT_CFG, "extended": False}
    features = build_book_features(df, cfg)
    cols = (FEATURE_COLUMNS_EXTENDED if cfg["extended"]
            else FEATURE_COLUMNS_BASE)
    features = features[cols]
    normed = apply_normalization(features, norm_params)
    target = build_multi_horizon_target(df, [int(h) for h in horizons])
    X, _, idxs = make_windowed_samples(normed, target, int(window))

    ratios = (cfg["split"]["train"], cfg["split"]["valid"], cfg["split"]["test"])
    _, va_end = split_indices_time_ordered(len(idxs), ratios)
    X_test, bars_test = X[va_end:], idxs[va_end:]
    if len(bars_test) == 0:
        raise ValueError("empty test slice")

    probs = {name: model_probability(net.forward(X_test))
             for name, net in nets.items()}
    votes = ensemble_vote(probs, trade_level=trade_level,
                          min_agreement=min_agreement)["samples"]

    atr = _atr_series(df, int(cfg["atr_period"]))
    opens = np.asarray(df["open"], dtype=float)
    closes = np.asarray(df["close"], dtype=float)

    trades: list[dict] = []
    rejected_while_busy = 0
    unfillable = 0
    fired = 0
    busy_until = -1                     # exit_bar of the open trade
    for k, vote in enumerate(votes):
        sig = vote["signal"]
        if sig == "flat":
            continue
        fired += 1
        b = int(bars_test[k])
        if b <= busy_until:             # position still open (EA mirror)
            rejected_while_busy += 1
            continue
        direction = 1 if sig == "long" else -1
        trade = _simulate_trade(df, b, direction, float(atr.iloc[b]),
                                int(horizon_bars), atr_mult, risk_reward,
                                spread)
        if trade is None:
            unfillable += 1
            continue
        trades.append(trade)
        busy_until = trade["exit_bar"]

    pnl_list = [t["pnl"] for t in trades]
    # balance-based equity curve (PnL is in price units per 1 unit of the
    # asset; the balance is expressed in the same units) - a relative DD on
    # a bare cumulative-PnL curve would be meaningless (it starts at 0)
    equity = (initial_balance
              + np.cumsum([0.0] + pnl_list)).tolist()
    peak, max_dd_pct = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - v) / peak * 100.0)

    metrics = forward_metrics(pnl_list)
    decision = evaluate_model_acceptance(metrics)
    score = score_from_trades(pnl_list, equity)

    test_start_bar = int(bars_test[0])
    buy_hold = (float(closes[-1]) - float(closes[test_start_bar])) \
        / float(closes[test_start_bar])

    return {
        "asset": asset, "timeframe": timeframe,
        "models": sorted(probs),
        "models_dir": models_dir,
        "bars": int(len(df)),
        "test_samples": int(len(bars_test)),
        "test_first_bar": int(test_start_bar),
        "config": {"trade_level": trade_level, "min_agreement": min_agreement,
                   "window": window, "horizons": list(horizons),
                   "horizon_bars": horizon_bars, "atr_mult": atr_mult,
                   "risk_reward": risk_reward, "spread": spread,
                   "initial_balance": initial_balance, "max_bars": max_bars},
        "signals_fired": fired,
        "rejected_while_busy": rejected_while_busy,
        "unfillable_tail_signals": unfillable,
        "trades": trades,
        "metrics": metrics,
        "initial_balance": initial_balance,
        "final_equity": round(float(equity[-1]), 4),
        "max_dd_pct": round(float(max_dd_pct), 4),
        "criterion_score": (None if score is None or not np.isfinite(score)
                            else round(float(score), 4)),
        "acceptance": {"accepted": bool(decision.accepted),
                       "reasons": decision.reasons,
                       "checks": decision.checks},
        "buy_hold_return_test_window": round(float(buy_hold), 6),
        "note": ("acceptance verdict by backtest.validation_protocol (T-03); "
                 "criterion by backtest.tester_criterion (T-08); only the "
                 "chronological test slice was traded"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models-dir", default="output/book_experiments_real")
    ap.add_argument("--db", default="data/market_data_external.sqlite")
    ap.add_argument("--asset", default="XAUUSD")
    ap.add_argument("--timeframe", default="M15")
    ap.add_argument("--max-bars", type=int, default=80000,
                    help="must equal the training run's --max-bars")
    ap.add_argument("--trade-level", type=float, default=0.6)
    ap.add_argument("--min-agreement", type=float, default=0.6)
    ap.add_argument("--atr-mult", type=float, default=1.5)
    ap.add_argument("--risk-reward", type=float, default=2.0)
    ap.add_argument("--spread", type=float, default=0.30,
                    help="round-trip spread in price units (XAUUSD ~$0.30)")
    ap.add_argument("--initial-balance", type=float, default=100.0,
                    help="backtest balance in price units (DD reference)")
    ap.add_argument("--horizon-bars", type=int, default=12)
    ap.add_argument("--out", default="output/book_ensemble_backtest/report.json")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    report = run_ensemble_backtest(
        args.models_dir, args.db, args.asset, args.timeframe,
        trade_level=args.trade_level, min_agreement=args.min_agreement,
        atr_mult=args.atr_mult, risk_reward=args.risk_reward,
        spread=args.spread, max_bars=args.max_bars,
        initial_balance=args.initial_balance,
        horizon_bars=args.horizon_bars)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    m = report["metrics"]
    print(json.dumps({
        "test_samples": report["test_samples"],
        "signals_fired": report["signals_fired"],
        "rejected_while_busy": report["rejected_while_busy"],
        "trades": m["trades"],
        "profit_factor": (None if m["profit_factor"] == float("inf")
                          else round(m["profit_factor"], 3)),
        "win_rate": round(m["win_rate"], 4),
        "net_pnl": round(m["net"], 3),
        "max_dd_pct": report["max_dd_pct"],
        "criterion_score": report["criterion_score"],
        "buy_hold_return": report["buy_hold_return_test_window"],
        "accepted": report["acceptance"]["accepted"],
        "reasons": report["acceptance"]["reasons"],
    }, indent=2))
    logger.info("report -> %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
