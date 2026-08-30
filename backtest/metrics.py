"""
Advanced Institutional Backtest Performance Metrics:
Win Rate, Profit Factor, Sharpe Ratio, Sortino Ratio, Expectancy, Drawdown, Max Consec Loss.
PnL Concentration Report (quant audit Section 5 / Task 3).
AUC Translator for Meta-Labeling / PF Targets (quant audit Section 5 / Task 9).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats


def trades_to_dataframe(trades) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=[
                "entry_ts",
                "exit_ts",
                "direction",
                "session",
                "regime_at_entry",
                "pnl",
                "exit_reason",
                "entry_price",
                "initial_stop_price",
                "tp1_price",
                "volume",
            ]
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

    # Sharpe & Sortino (annualized by the ACTUAL per-year trade frequency when the
    # entry timestamps are available, else fall back to ~250 trading days).
    # T7 (audit 2026-08-10): the previous hard-coded sqrt(250) made per-trade
    # Sharpe/Sortino incomparable across assets with very different trade counts
    # (XAU ~2200 trades/yr vs EUR on H1 an order of magnitude fewer), so a
    # cross-asset table was meaningless. Annualizing by the realized trade
    # frequency yields a common scale: sharpe = mean/std * sqrt(trades_per_year).
    annual = 250.0
    if "entry_ts" in trades_df.columns and len(trades_df) >= 2:
        ts = trades_df["entry_ts"].to_numpy(dtype=float)
        span_secs = float(ts.max() - ts.min())
        if span_secs > 0 and np.isfinite(span_secs):
            span_years = span_secs / (365.25 * 86400.0)
            tpy = len(trades_df) / span_years if span_years > 0 else 250.0
            if tpy > 0 and np.isfinite(tpy):
                annual = tpy

    mean_pnl = pnls.mean()
    std_pnl = pnls.std()
    sharpe_ratio = (mean_pnl / std_pnl * np.sqrt(annual)) if std_pnl > 0 else 0.0

    downside_pnls = pnls[pnls < 0]
    downside_std = downside_pnls.std() if len(downside_pnls) > 0 else 0.0
    sortino_ratio = (mean_pnl / downside_std * np.sqrt(annual)) if downside_std > 0 else 0.0

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


def compute_r_metrics(trades_df: pd.DataFrame, point_value_lot: float = 1.0, volume: float = 0.01) -> dict:
    """R-normalized performance summary + exit-path bucket table.

    Requires entry_price / initial_stop_price columns (trades_to_dataframe
    provides them for EnsembleBacktester trades). Missing risk -> row dropped
    from the R calculations (NaN-safe). Buckets are keyed by exit_reason and
    report count, share, mean R and the contribution of the bucket to the
    total R sum.
    """
    empty = {
        "n": 0,
        "mean_r": 0.0,
        "std_r": 0.0,
        "skew_r": 0.0,
        "kurtosis_excess_r": 0.0,
        "avg_win_r": 0.0,
        "avg_loss_r": 0.0,
        "breakeven_wr_pct": float("nan"),
        "actual_wr_pct": float("nan"),
        "net_expectancy_r": 0.0,
        "buckets": {},
    }
    if trades_df is None or len(trades_df) == 0:
        return empty
    if "initial_stop_price" not in trades_df.columns or "entry_price" not in trades_df.columns:
        return empty

    tdf = trades_df.copy()
    tdf["risk_price"] = (tdf["entry_price"] - tdf["initial_stop_price"]).abs()
    # W7: when the trade frame carries a per-trade `volume` column (as
    # trades_to_dataframe does for EnsembleBacktester trades), honour it instead
    # of the single scalar `volume` default that matches no asset. Different
    # instruments use different lot sizes, so a scalar default silently scales
    # R by the wrong multiplier.
    if "volume" in tdf.columns and tdf["volume"].notna().any():
        vol = tdf["volume"].fillna(volume)
    else:
        vol = pd.Series(float(volume), index=tdf.index)
    tdf["risk_money"] = tdf["risk_price"] * vol.astype(float) * point_value_lot
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
        sample = np.concatenate([arr[s : s + block] for s in starts])[:n]
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
    from scipy.stats import binomtest

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
    pfs = [r["profit_factor"] for r in valid if np.isfinite(r.get("profit_factor", np.nan))]
    median_pf = float(np.median(pfs)) if pfs else float("nan")
    inconsistent = bool(valid and np.isfinite(median_pf) and median_pf > 1.0 and (len(pos_valid) / len(valid)) < 0.5)
    return {
        "n_folds": len(results),
        "valid_folds": len(valid),
        "positive_folds": len(pos_all),
        "positive_folds_valid": len(pos_valid),
        "positive_folds_pct_valid": round(100.0 * len(pos_valid) / len(valid), 1) if valid else 0.0,
        "median_pf_valid": round(median_pf, 3) if np.isfinite(median_pf) else None,
        "inconsistent": inconsistent,
        "note": (
            "PF/PnL statistics refer to different fold sets (empty folds counted in one, excluded from the other)"
            if inconsistent
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Task 3: PnL Concentration Metrics (KIMI K3 / Quant Audit Section 5)
# ---------------------------------------------------------------------------


def pnl_concentration_report(
    trades_df: pd.DataFrame,
    top5_threshold: float = 0.35,
    fold_threshold: float = 0.30,
) -> dict:
    """Computes profit concentration metrics and evaluates red flags.

    Parameters
    ----------
    trades_df : pd.DataFrame
        DataFrame of executed trades with 'pnl' (or 'net_r'), 'fold_id' (or 'fold'/'window'),
        and 'date' (or 'entry_ts'/'timestamp_utc').
    top5_threshold : float
        Red-flag threshold for share of top-5 winning trades (default 0.35 = 35%).
    fold_threshold : float
        Red-flag threshold for share of single best fold in total PnL (default 0.30 = 30%).

    Returns
    -------
    dict with:
      - top5_share: fraction of total positive PnL coming from the top 5 winning trades
      - top5_pnl: sum of top 5 trades PnL
      - top5_flag: bool, True if top5_share > top5_threshold
      - best_fold_share: fraction of total PnL contributed by the single best fold
      - best_fold_pnl: PnL of the best fold
      - best_fold_id: identifier of the best fold
      - best_fold_flag: bool, True if best_fold_share > fold_threshold
      - worst_day_pnl: lowest single-day aggregated PnL
      - worst_day_date: date of the worst day
      - total_pnl: total aggregate PnL
      - has_red_flags: bool, True if any concentration threshold was exceeded
    """
    empty = {
        "top5_share": 0.0,
        "top5_pnl": 0.0,
        "top5_threshold": top5_threshold,
        "top5_flag": False,
        "best_fold_share": 0.0,
        "best_fold_pnl": 0.0,
        "best_fold_id": None,
        "fold_threshold": fold_threshold,
        "best_fold_flag": False,
        "worst_day_pnl": 0.0,
        "worst_day_date": None,
        "total_pnl": 0.0,
        "has_red_flags": False,
    }
    if trades_df is None or len(trades_df) == 0:
        return empty

    tdf = trades_df.copy()
    pnl_col = "pnl" if "pnl" in tdf.columns else ("net_r" if "net_r" in tdf.columns else None)
    if not pnl_col:
        return empty

    pnls = tdf[pnl_col].values.astype(float)
    total_pnl = float(pnls.sum())
    wins = pnls[pnls > 0]
    gross_profit = float(wins.sum()) if len(wins) > 0 else 0.0

    # 1. Share of top-5 winning trades in gross profit
    if gross_profit > 0 and len(wins) > 0:
        sorted_wins = np.sort(wins)[::-1]
        top5_pnl = float(sorted_wins[: min(5, len(sorted_wins))].sum())
        top5_share = top5_pnl / gross_profit
    elif total_pnl > 0 and len(pnls) > 0:
        sorted_pnls = np.sort(pnls)[::-1]
        top5_pnl = float(sorted_pnls[: min(5, len(sorted_pnls))].sum())
        top5_share = top5_pnl / total_pnl
    else:
        top5_pnl = 0.0
        top5_share = 0.0

    top5_flag = bool(top5_share > top5_threshold)

    # 2. Contribution of best fold to total PnL
    fold_col = next((c for c in ["fold_id", "fold", "window", "fold_idx"] if c in tdf.columns), None)
    if fold_col is not None and tdf[fold_col].nunique() > 1:
        fold_sums = tdf.groupby(fold_col)[pnl_col].sum()
        best_fold_id = fold_sums.idxmax()
        best_fold_pnl = float(fold_sums.max())
        if total_pnl > 0:
            best_fold_share = best_fold_pnl / total_pnl
        elif best_fold_pnl > 0:
            best_fold_share = 1.0
        else:
            best_fold_share = 0.0
    else:
        best_fold_id = "fold_0" if fold_col else None
        best_fold_pnl = total_pnl
        best_fold_share = 1.0 if total_pnl > 0 else 0.0

    best_fold_flag = bool(best_fold_share > fold_threshold and (fold_col is not None and tdf[fold_col].nunique() > 1))

    # 3. Worst day by daily PnL aggregation
    date_col = next((c for c in ["date", "entry_date", "day"] if c in tdf.columns), None)
    if date_col is None:
        ts_col = next((c for c in ["entry_ts", "timestamp_utc", "exit_ts"] if c in tdf.columns), None)
        if ts_col is not None:
            tdf["_computed_date"] = pd.to_datetime(tdf[ts_col], unit="s", utc=True).dt.date
            date_col = "_computed_date"

    if date_col is not None:
        daily_sums = tdf.groupby(date_col)[pnl_col].sum()
        worst_day_pnl = float(daily_sums.min())
        worst_day_date = str(daily_sums.idxmin())
    else:
        worst_day_pnl = float(pnls.min()) if len(pnls) > 0 else 0.0
        worst_day_date = None

    has_red_flags = bool(top5_flag or best_fold_flag)

    return {
        "top5_share": round(float(top5_share), 4),
        "top5_pnl": round(float(top5_pnl), 2),
        "top5_threshold": float(top5_threshold),
        "top5_flag": top5_flag,
        "best_fold_share": round(float(best_fold_share), 4),
        "best_fold_pnl": round(float(best_fold_pnl), 2),
        "best_fold_id": best_fold_id,
        "fold_threshold": float(fold_threshold),
        "best_fold_flag": best_fold_flag,
        "worst_day_pnl": round(float(worst_day_pnl), 2),
        "worst_day_date": worst_day_date,
        "total_pnl": round(float(total_pnl), 2),
        "has_red_flags": has_red_flags,
    }


# ---------------------------------------------------------------------------
# Task 9: AUC Translator for PF Targets (Signal Detection Theory / Quant Audit)
# ---------------------------------------------------------------------------


def required_auc_for_pf_target(
    pf_current: float,
    pf_target: float,
    win_rate: float = 0.517,
    avg_win_r: float = 1.0,
    sigma_r: float = 0.40,
    cutoff_fraction: float = 0.40,
) -> dict:
    """Translates a target Profit Factor (PF) increase into the required AUC
    on purged-OOS predictions using Signal Detection Theory (Green & Swets 1966,
    AFML audit translation: d' = sqrt(2) * Phi^-1(AUC)).

    Parameters
    ----------
    pf_current : float
        Current baseline Profit Factor (e.g. 1.07).
    pf_target : float
        Target Profit Factor after filtering (e.g. 1.21).
    win_rate : float
        Current win rate (fraction in (0, 1), default 0.517).
    avg_win_r : float
        Average win magnitude in R (default 1.0).
    sigma_r : float
        Standard deviation of trade return in R units (default 0.40).
    cutoff_fraction : float
        Fraction of lowest-scoring trades removed by the filter (default 0.40,
        i.e. retaining top 60% of trades: N=1000 -> 600).

    Returns
    -------
    dict with:
      - required_auc: float in [0.50, 1.00]
      - d_prime: sensitivity index d' = sqrt(2) * Phi^-1(AUC)
      - delta_expectancy_r: required improvement in average trade R
      - realistic: bool, True if required_auc is in realistic range (0.50..0.58)
      - verdict: 'realistic' (0.50-0.58), 'unlikely_high' (>0.58), or 'already_achieved' (<=0.50)
    """
    if pf_target <= pf_current:
        return {
            "required_auc": 0.50,
            "d_prime": 0.0,
            "delta_expectancy_r": 0.0,
            "realistic": True,
            "verdict": "already_achieved",
        }

    p0 = float(win_rate)
    W = float(avg_win_r)
    # Baseline average loss L from current PF: PF = (p0 * W) / ((1 - p0) * L)
    L = (p0 * W) / ((1.0 - p0) * max(pf_current, 1e-6))

    # Target win rate p1 needed to reach pf_target assuming W/L ratio approx preserved
    p1 = (pf_target * L) / (W + pf_target * L)
    delta_p = p1 - p0
    delta_mu_r = delta_p * (W + L)

    # Selection intensity i_c for standard normal truncated at cutoff c
    c = float(np.clip(cutoff_fraction, 0.01, 0.99))
    z_c = float(stats.norm.ppf(c))
    phi_z_c = float(stats.norm.pdf(z_c))
    i_c = phi_z_c / (1.0 - c)

    # In Signal Detection Theory, d' = sqrt(2) * Phi^-1(AUC)
    # The expected improvement delta_mu_r = i_c * sigma_r * rho
    # with rho = d' / sqrt(d'^2 + 2) or linear approximation d' = delta_mu_r / (i_c * sigma_r)
    denom = i_c * float(sigma_r)
    d_prime = delta_mu_r / max(denom, 1e-12) if denom > 0 else 0.0

    if d_prime <= 0.0:
        auc = 0.50
    elif d_prime >= 8.0:
        auc = 0.9999
    else:
        # AUC = Phi(d' / sqrt(2)) from d' = sqrt(2) * Phi^-1(AUC)
        auc = float(stats.norm.cdf(d_prime / math.sqrt(2.0)))

    # Realistic bounds for low SNR financial time series (0.50..0.58 realistic, >0.58 unlikely)
    realistic = bool(0.50 <= auc <= 0.58)
    if auc <= 0.50:
        verdict = "already_achieved"
    elif 0.50 < auc <= 0.58:
        verdict = "realistic"
    else:
        verdict = "unlikely_high"

    return {
        "required_auc": round(float(auc), 4),
        "d_prime": round(float(d_prime), 4),
        "delta_expectancy_r": round(float(delta_mu_r), 4),
        "target_win_rate": round(float(p1), 4),
        "realistic": realistic,
        "verdict": verdict,
    }


def progress_pnl_curve(
    df: pd.DataFrame,
    trades: list,
    max_bars: int = 36,
) -> pd.DataFrame:
    """Computes cumulative and average PnL for trades remaining open at each
    holding bar N in [1, max_bars].

    Allows setting the progress-stop threshold from the empirical curve shape
    rather than arbitrary picking (quant audit Section 5 / Task 6).
    """
    if not trades or len(df) == 0:
        return pd.DataFrame(columns=["bar", "n_open", "cum_pnl", "mean_pnl", "mean_progress_atr"])

    # Discretize timestamps to bar index
    ts_values = df["timestamp_utc"].values
    ts_to_idx = {ts: idx for idx, ts in enumerate(ts_values)}
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    atrs = df["atr"].values if "atr" in df.columns else np.ones(len(df))

    records = []
    for bar_k in range(1, max_bars + 1):
        bar_pnls = []
        bar_progress = []

        for t in trades:
            entry_ts = getattr(t, "entry_ts", None)
            exit_ts = getattr(t, "exit_ts", None)
            direction = getattr(t, "direction", 1)
            entry_price = getattr(t, "entry_price", None)
            if entry_ts not in ts_to_idx or entry_price is None:
                continue

            e_idx = ts_to_idx[entry_ts]
            x_idx = ts_to_idx.get(exit_ts, len(df) - 1)
            held = x_idx - e_idx

            if held >= bar_k:
                curr_idx = min(e_idx + bar_k, len(df) - 1)
                pnl_k = direction * (closes[curr_idx] - entry_price)
                atr_k = atrs[curr_idx] if curr_idx < len(atrs) and not np.isnan(atrs[curr_idx]) else 1.0
                fav_move = (highs[curr_idx] - entry_price) if direction == 1 else (entry_price - lows[curr_idx])
                bar_pnls.append(pnl_k)
                bar_progress.append(fav_move / max(atr_k, 1e-6))

        n_open = len(bar_pnls)
        if n_open > 0:
            records.append(
                {
                    "bar": bar_k,
                    "n_open": n_open,
                    "cum_pnl": round(float(np.sum(bar_pnls)), 2),
                    "mean_pnl": round(float(np.mean(bar_pnls)), 4),
                    "mean_progress_atr": round(float(np.mean(bar_progress)), 4),
                }
            )
        else:
            records.append(
                {
                    "bar": bar_k,
                    "n_open": 0,
                    "cum_pnl": 0.0,
                    "mean_pnl": 0.0,
                    "mean_progress_atr": 0.0,
                }
            )

    return pd.DataFrame(records)
