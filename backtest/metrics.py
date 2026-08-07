"""
Advanced Institutional Backtest Performance Metrics:
Win Rate, Profit Factor, Sharpe Ratio, Sortino Ratio, Expectancy, Drawdown, Max Consec Loss.
"""
import numpy as np
import pandas as pd
from typing import List


def trades_to_dataframe(trades) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=["entry_ts", "exit_ts", "direction", "session", "regime_at_entry",
                     "pnl", "exit_reason", "entry_price", "initial_stop_price",
                     "tp1_price", "volume"]
        )
    return pd.DataFrame(
        [
            {
                "entry_ts": t.entry_ts,
                "exit_ts": t.exit_ts,
                "direction": t.direction,
                "session": t.session,
                "regime_at_entry": t.regime_at_entry,
                "pnl": t.pnl,
                "exit_reason": t.exit_reason,
                # R-multiplicator support (quant audit 2026-08-07): entry and the
                # ORIGINAL stop (before any BE/trailing move) let every consumer
                # normalize PnL by the risk actually taken. getattr keeps
                # backward compatibility with trade objects that lack the fields.
                "entry_price": getattr(t, "entry_price", None),
                "initial_stop_price": getattr(t, "initial_stop_price", None),
                "tp1_price": getattr(t, "tp1_price", None),
                "volume": getattr(t, "volume", None),
            }
            for t in trades
        ]
    )


def compute_metrics(trades_df: pd.DataFrame) -> dict:
    if len(trades_df) == 0:
        return {
            "n_trades": 0,
            "win_rate": np.nan,
            "profit_factor": np.nan,
            "sharpe_ratio": np.nan,
            "sortino_ratio": np.nan,
            "expectancy": 0.0,
            "max_drawdown": 0.0,
            "total_pnl": 0.0,
            "max_consecutive_losses": 0,
        }

    pnls = trades_df["pnl"].values
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    gross_profit = wins.sum() if len(wins) > 0 else 0.0
    gross_loss = -losses.sum() if len(losses) > 0 else 0.0

    win_rate = (len(wins) / len(pnls)) * 100.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.inf

    # Expectancy ($ per trade)
    expectancy = pnls.mean()

    # Sharpe & Sortino (Annualized based on ~250 trading days)
    mean_pnl = pnls.mean()
    std_pnl = pnls.std()
    sharpe_ratio = (mean_pnl / std_pnl * np.sqrt(250)) if std_pnl > 0 else 0.0

    downside_pnls = pnls[pnls < 0]
    downside_std = downside_pnls.std() if len(downside_pnls) > 0 else 0.0
    sortino_ratio = (mean_pnl / downside_std * np.sqrt(250)) if downside_std > 0 else 0.0

    # Drawdown
    cum_pnl = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = cum_pnl - running_max
    max_drawdown = float(drawdowns.min()) if len(drawdowns) > 0 else 0.0

    # Max Consecutive Losses
    max_consec_loss = 0
    current_consec = 0
    for pnl in pnls:
        if pnl <= 0:
            current_consec += 1
            if current_consec > max_consec_loss:
                max_consec_loss = current_consec
        else:
            current_consec = 0

    return {
        "n_trades": len(trades_df),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2) if not np.isinf(profit_factor) else 999.0,
        "sharpe_ratio": round(sharpe_ratio, 2),
        "sortino_ratio": round(sortino_ratio, 2),
        "expectancy": round(expectancy, 2),
        "max_drawdown": round(max_drawdown, 2),
        "total_pnl": round(float(cum_pnl[-1]), 2),
        "max_consecutive_losses": max_consec_loss,
    }


def compute_metrics_per_session(trades_df: pd.DataFrame) -> dict:
    """
    Compute full metrics broken down by session label.

    Returns a dict keyed by session name -> metrics dict (the output of
    compute_metrics() for that session's trades). Sessions with no trades are
    omitted entirely, so every returned entry has n_trades >= 1.
    """
    if trades_df is None or len(trades_df) == 0 or "session" not in trades_df.columns:
        return {}
    per_session: dict = {}
    for session_name, group in trades_df.groupby("session"):
        per_session[str(session_name)] = compute_metrics(group.reset_index(drop=True))
    return per_session

# ---------------------------------------------------------------------------
# R-multiplicator metrics (quant audit 2026-08-07, Claude 5 Opus plan)
#
# R = trade PnL (money) / risk at entry (money) where risk = |entry - initial
# stop| * volume * point_value_lot. The grid geometry caps R at +0.567 (full
# TP3 with 50/30/20) and floors it at -1.0, so sigma(R) ~ 0.35-0.45 and the
# detectability table in the audit follows from it. All cross-asset
# comparisons MUST be done in R, never in raw money (different tick value,
# stop distance, trade count).
# ---------------------------------------------------------------------------

def compute_r_metrics(trades_df: pd.DataFrame, point_value_lot: float = 1.0,
                      volume: float = 0.01) -> dict:
    """R-normalized performance summary + exit-path bucket table.

    Requires entry_price / initial_stop_price columns (trades_to_dataframe
    provides them for EnsembleBacktester trades). Missing risk -> row dropped
    from the R calculations (NaN-safe). Buckets are keyed by exit_reason and
    report count, share, mean R and the contribution of the bucket to the
    total R sum.
    """
    empty = {"n": 0, "mean_r": 0.0, "std_r": 0.0, "skew_r": 0.0, "kurtosis_excess_r": 0.0,
             "avg_win_r": 0.0, "avg_loss_r": 0.0, "breakeven_wr_pct": float("nan"),
             "actual_wr_pct": float("nan"), "net_expectancy_r": 0.0, "buckets": {}}
    if trades_df is None or len(trades_df) == 0:
        return empty
    if "initial_stop_price" not in trades_df.columns or "entry_price" not in trades_df.columns:
        return empty

    tdf = trades_df.copy()
    tdf["risk_price"] = (tdf["entry_price"] - tdf["initial_stop_price"]).abs()
    tdf["risk_money"] = tdf["risk_price"] * volume * point_value_lot
    tdf = tdf[tdf["risk_money"] > 1e-12]
    if len(tdf) == 0:
        return empty

    r = (tdf["pnl"] / tdf["risk_money"]).to_numpy(dtype=float)
    wins = r[r > 0]
    losses = r[r <= 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0  # positive magnitude

    buckets = {}
    total_r = float(r.sum())
    for reason, g in tdf.groupby("exit_reason"):
        gr = (g["pnl"] / g["risk_money"]).to_numpy(dtype=float)
        buckets[str(reason)] = {
            "n": int(len(g)),
            "share_pct": round(100.0 * len(g) / len(tdf), 1),
            "mean_r": round(float(gr.mean()), 4),
            "r_contribution_pct": float(round(100.0 * gr.sum() / total_r, 1)) if total_r != 0 else 0.0,
        }

    skew_r = float(_safe_skew(r))
    kurt_r = float(_safe_kurt(r))
    be_wr = 100.0 * avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else float("nan")
    # Profit concentration (audit Q7): share of total R from the best 5%/1% trades.
    top5 = 0.0
    top1 = 0.0
    total_r = float(r.sum())
    if total_r > 0 and len(r) >= 20:
        srt = np.sort(r)[::-1]
        top5 = float(srt[: max(1, len(srt) // 20)].sum()) / total_r
        top1 = float(srt[: max(1, len(srt) // 100)].sum()) / total_r
    return {
        "n": int(len(tdf)),
        "mean_r": round(float(r.mean()), 4),
        "std_r": round(float(r.std(ddof=1)), 4) if len(r) > 1 else 0.0,
        "skew_r": round(skew_r, 4),
        "kurtosis_excess_r": round(kurt_r, 4),
        "avg_win_r": round(avg_win, 4),
        "avg_loss_r": round(avg_loss, 4),
        "breakeven_wr_pct": round(be_wr, 1),
        "actual_wr_pct": round(100.0 * float(np.mean(r > 0)), 1),
        "net_expectancy_r": round(float(r.mean()), 4),
        "top5_concentration_pct": round(100.0 * top5, 1),
        "top1_concentration_pct": round(100.0 * top1, 1),
        "buckets": buckets,
    }


def _safe_skew(r: np.ndarray) -> float:
    if len(r) < 3:
        return 0.0
    return float(pd.Series(r).skew())


def _safe_kurt(r: np.ndarray) -> float:
    if len(r) < 4:
        return 0.0
    return float(pd.Series(r).kurtosis())  # Fisher excess (normal = 0)


def block_bootstrap_t(r, block: int = 20, n_boot: int = 10000, seed: int = 0) -> float:
    """Block-bootstrap t-statistic for the mean of R-multiplicators.

    Plain iid t is inflated by clustering (overlapping holding horizons,
    same-day/regime trades). Blocks of length = max holding horizon preserve
    the serial dependence; the bootstrap standard error is then honest.
    Returns 0.0 for degenerate inputs.
    """
    arr = np.asarray(r, dtype=float)
    n = len(arr)
    if n < 2:
        return 0.0
    # A block longer than the sample would make rng.integers(0, n - block)
    # fail with high <= 0; shrink the block to n - 1 (single block = whole
    # series re-sampled by block starts).
    block = max(1, min(int(block), n - 1))
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    means = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n - block, size=nb)
        sample = np.concatenate([arr[s:s + block] for s in starts])[:n]
        means[i] = sample.mean()
    std = means.std(ddof=1)
    if std == 0.0 or not np.isfinite(std):
        return 0.0
    return float(arr.mean() / std)


def fold_sign_test(n_positive_folds: int, n_folds: int) -> dict:
    """One-sided binomial sign test on positive-fold count (H0: p = 0.5).

    Uses the exact binomial test (scipy) with the continuity-corrected normal
    z for reporting. This is the audit's 'знаковый тест по фолдам'.
    """
    from scipy.stats import binomtest, norm
    if n_folds <= 0:
        return {"z": float("nan"), "p_one_sided": float("nan"), "n_positive": 0, "n_folds": 0}
    z = (n_positive_folds - 0.5 * n_folds) / (0.5 * np.sqrt(n_folds)) if n_folds > 0 else float("nan")
    res = binomtest(int(n_positive_folds), int(n_folds), 0.5, alternative="greater")
    return {
        "z": round(float(z), 3),
        "p_one_sided": round(float(res.pvalue), 4),
        "n_positive": int(n_positive_folds),
        "n_folds": int(n_folds),
    }


def summarize_folds(results: list) -> dict:
    """Aggregate walk-forward fold results with an arithmetic-consistency check.

    Quant audit 0.1: 'PF_med ~1.07 with 19/41 positive folds' is impossible if
    both statistics refer to the same fold set (PF > 1 <=> fold PnL > 0), so
    the check reports positive folds over VALID (non-empty) folds and flags
    the mismatch that appears when empty folds are counted in one statistic
    but excluded from the other.
    """
    valid = [r for r in results if r.get("n_trades", 0) > 0]
    pos_all = [r for r in results if r.get("total_pnl", 0.0) > 0]
    pos_valid = [r for r in valid if r.get("total_pnl", 0.0) > 0]
    pfs = [r["profit_factor"] for r in valid
           if np.isfinite(r.get("profit_factor", np.nan))]
    median_pf = float(np.median(pfs)) if pfs else float("nan")
    inconsistent = bool(
        valid and np.isfinite(median_pf) and median_pf > 1.0
        and (len(pos_valid) / len(valid)) < 0.5
    )
    return {
        "n_folds": len(results),
        "valid_folds": len(valid),
        "positive_folds": len(pos_all),
        "positive_folds_valid": len(pos_valid),
        "positive_folds_pct_valid": round(100.0 * len(pos_valid) / len(valid), 1) if valid else 0.0,
        "median_pf_valid": round(median_pf, 3) if np.isfinite(median_pf) else None,
        "inconsistent": inconsistent,
        "note": ("PF/PnL statistics refer to different fold sets (empty folds counted in one, "
                 "excluded from the other)" if inconsistent else None),
    }
