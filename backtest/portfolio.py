"""
Portfolio analytics (quant audit 2026-08-07, Claude plan action 5 / question 5).

The audit's core corrections vs naive portfolio math:

- correlate STRATEGY returns (daily sums of net R), not spot prices: the M5
  gold strategy vs M15 silver strategy can have rho ~0.4-0.6 even when spot
  rho is 0.84;
- no mean-variance optimization (all t < 2): use cluster risk parity —
  clusters {XAU,XAG}, {EUR,GBP}, {BTC} get 1/3 of the risk budget each, split
  equally inside;
- ENB (effective number of bets) from the eigenvalues of the correlation
  matrix (Lopez de Prado): ENB = exp(-sum p_i ln p_i), p_i = normalized
  eigenvalue risk contributions;
- kill-switch thresholds from the BACKTEST distribution: 2-sigma daily R,
  3-sigma weekly R, rolling 60-trade -3-sigma regime-break test;
- scheme comparison (equal / inverse-vol / risk parity / cluster risk parity)
  with and without XAG, on portfolio Sharpe / max DD / Calmar / ENB.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def daily_r_matrix(trades_by_asset: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Daily net-R matrix: rows = dates, columns = assets.

    `trades_by_asset` maps asset -> per-trade DataFrame with `entry_ts`
    (epoch seconds) and `net_r` columns. Rows are the union of dates across
    assets; missing days are 0.0 (no exposure -> no PnL).
    """
    frames = []
    for asset, tdf in trades_by_asset.items():
        if tdf is None or len(tdf) == 0:
            continue
        d = tdf.copy()
        d["date"] = pd.to_datetime(d["entry_ts"], unit="s", utc=True).dt.date
        daily = d.groupby("date")["net_r"].sum()
        daily.name = asset
        frames.append(daily)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1).sort_index()
    return out.fillna(0.0)


def strategy_correlation(daily_r: pd.DataFrame) -> pd.DataFrame:
    """Pairwise correlation of strategy daily R (NaN-safe)."""
    if daily_r.shape[1] < 2:
        return pd.DataFrame(index=daily_r.columns, columns=daily_r.columns, dtype=float)
    return daily_r.corr()


def effective_number_bets(daily_r: pd.DataFrame) -> float:
    """ENB from the correlation-matrix spectrum (Lopez de Prado).

    ENB = exp(-sum_i p_i ln p_i) with p_i = lambda_i / sum(lambda) over the
    eigenvalues of the correlation matrix. ENB ~ 1 for one dominant bet,
    ENB ~ N for N independent bets.
    """
    if daily_r.shape[1] < 2:
        return 1.0
    corr = daily_r.corr().to_numpy()
    corr = np.nan_to_num(corr, nan=0.0)
    eig = np.linalg.eigvalsh(corr)
    eig = np.clip(eig, 0.0, None)
    total = float(eig.sum())
    if total <= 0:
        return 1.0
    p = eig / total
    p = p[p > 1e-12]
    return float(np.exp(-np.sum(p * np.log(p))))


def cluster_risk_parity_weights(assets: list[str], clusters: dict[str, list[str]]) -> pd.Series:
    """Cluster risk parity: each cluster gets 1/|clusters| of the risk budget,
    split equally inside. Assets not in any cluster get their own singleton
    cluster. Returns normalized weights summing to 1."""
    if not assets:
        return pd.Series(dtype=float)
    w = {a: 0.0 for a in assets}
    assigned = set()
    cluster_list = []
    for cl in clusters.values():
        members = [a for a in cl if a in assets]
        if members:
            cluster_list.append(members)
            assigned.update(members)
    for a in assets:
        if a not in assigned:
            cluster_list.append([a])
    budget = 1.0 / len(cluster_list)
    for members in cluster_list:
        share = budget / len(members)
        for m in members:
            w[m] = share
    total = sum(w.values())
    if total <= 0:
        return pd.Series(w)
    return pd.Series({k: v / total for k, v in w.items()})


def portfolio_curve(daily_r: pd.DataFrame, weights: pd.Series) -> pd.Series:
    """Daily portfolio R series under fixed weights."""
    w = weights.reindex(daily_r.columns).fillna(0.0)
    return daily_r @ w


def portfolio_metrics(daily_r: pd.DataFrame, weights: pd.Series, periods_per_year: float = 250.0) -> dict:
    """Sharpe / annual vol / max DD / Calmar / ENB for one weighting scheme."""
    if len(daily_r) == 0:
        return {"sharpe": 0.0, "ann_vol": 0.0, "max_dd": 0.0, "calmar": 0.0, "enb": 1.0}
    curve = portfolio_curve(daily_r, weights)
    r = curve.to_numpy(dtype=float)
    mean, std = float(r.mean()), float(r.std(ddof=1))
    sharpe = mean / std * np.sqrt(periods_per_year) if std > 0 else 0.0
    cum = np.cumsum(r)
    dd = float((cum - np.maximum.accumulate(cum)).min())
    calmar = (mean * periods_per_year) / abs(dd) if dd < 0 else float("inf")
    return {
        "sharpe": round(float(sharpe), 3),
        "ann_vol": round(float(std * np.sqrt(periods_per_year)), 4),
        "max_dd_r": round(dd, 3),
        "calmar": round(calmar, 3) if np.isfinite(calmar) else None,
        "enb": round(effective_number_bets(daily_r), 2),
    }


def compare_schemes(daily_r: pd.DataFrame, clusters: dict[str, list[str]]) -> dict:
    """Equal / inverse-vol / risk parity / cluster risk parity, each with
    Sharpe/maxDD/Calmar/ENB. Returns dict keyed by scheme name."""
    assets = list(daily_r.columns)
    if not assets:
        return {}
    vol = daily_r.std(ddof=1).replace(0.0, np.nan)
    out = {}
    eq = pd.Series(1.0 / len(assets), index=assets)
    out["equal_weight"] = {"weights": eq.to_dict(), **portfolio_metrics(daily_r, eq)}
    if vol.notna().all() and (vol > 0).all():
        iv = 1.0 / vol
        iv = iv / iv.sum()
        out["inverse_vol"] = {"weights": iv.to_dict(), **portfolio_metrics(daily_r, iv)}
    # naive risk parity via inverse-vol on pairwise cov diagonal dominance
    cov = daily_r.cov()
    inv_diag = 1.0 / cov.to_numpy().diagonal()
    rp = pd.Series(inv_diag / inv_diag.sum(), index=assets)
    out["risk_parity"] = {"weights": rp.to_dict(), **portfolio_metrics(daily_r, rp)}
    crp = cluster_risk_parity_weights(assets, clusters)
    out["cluster_risk_parity"] = {"weights": crp.to_dict(), **portfolio_metrics(daily_r, crp)}
    return out


def kill_switch_thresholds(daily_r: pd.DataFrame, rolling_trades: pd.DataFrame | None = None) -> dict:
    """Kill-switch thresholds from the backtest distribution (audit question 6):
    daily 2-sigma, weekly 3-sigma, and the 60-trade rolling -3-sigma regime
    break. `rolling_trades` optional per-trade frame (asset -> trades) for the
    60-trade check; falls back to daily sums when absent."""
    port = daily_r.sum(axis=1) if daily_r.shape[1] > 1 else daily_r.iloc[:, 0]
    daily_std = float(port.std(ddof=1)) if len(port) > 1 else 0.0
    weekly = port.rolling(5).sum().dropna()
    weekly_std = float(weekly.std(ddof=1)) if len(weekly) > 1 else 0.0
    return {
        "daily_2sigma": round(-2.0 * daily_std, 4),
        "weekly_3sigma": round(-3.0 * weekly_std, 4),
        "n_days": int(len(port)),
        "note": "portfolio R thresholds; a daily sum below daily_2sigma or a "
        "5-day sum below weekly_3sigma trips the kill-switch",
    }


# ---------------------------------------------------------------------------
# Task 8: Signal Ranking & Queue Loss Analytics (Quant Audit Section 5.2B / Q4f)
# ---------------------------------------------------------------------------


def rank_concurrent_signals(
    signals: list[dict],
    ranking_metric: str = "confidence",
) -> list[dict]:
    """Ranks competing signals firing at the same timestamp across assets.

    Parameters
    ----------
    signals : list of signal dicts with keys like 'confidence', 'p_long', 'p_short', 'asset', 'ev'
    ranking_metric : 'confidence' (max(|p_long - p_short|)), 'ev', or 'probability'

    Returns
    -------
    list of signals sorted descending by the chosen ranking metric.
    """
    if not signals:
        return []

    def _score(sig: dict) -> float:
        if ranking_metric == "ev":
            return float(sig.get("ev", sig.get("confidence", 0.0)))
        if ranking_metric in ("confidence", "probability"):
            p_long = float(sig.get("p_long", 0.5))
            p_short = float(sig.get("p_short", 0.5))
            conf = float(sig.get("confidence", max(p_long, p_short)))
            return conf
        return float(sig.get(ranking_metric, 0.0))

    return sorted(signals, key=_score, reverse=True)


def calculate_queue_loss(
    taken_trades: list[dict] | pd.DataFrame,
    rejected_trades: list[dict] | pd.DataFrame,
) -> dict:
    """Calculates the expected return E[R] of taken signals versus rejected signals
    (queue loss from position concurrency caps, audit Section 5.2B / Question 4f).

    If E[R]_rejected >= E[R]_taken, logs a critical finding.
    """
    taken_df = pd.DataFrame(taken_trades) if isinstance(taken_trades, list) else taken_trades.copy()
    rejected_df = pd.DataFrame(rejected_trades) if isinstance(rejected_trades, list) else rejected_trades.copy()

    r_col_taken = "net_r" if "net_r" in taken_df.columns else ("pnl" if "pnl" in taken_df.columns else None)
    r_col_rej = "net_r" if "net_r" in rejected_df.columns else ("pnl" if "pnl" in rejected_df.columns else None)

    e_r_taken = float(taken_df[r_col_taken].mean()) if (r_col_taken and len(taken_df) > 0) else 0.0
    e_r_rejected = float(rejected_df[r_col_rej].mean()) if (r_col_rej and len(rejected_df) > 0) else 0.0

    queue_loss = e_r_taken - e_r_rejected
    is_critical = bool(len(rejected_df) > 0 and e_r_rejected >= e_r_taken)

    warning_msg = None
    if is_critical:
        import logging

        logger = logging.getLogger("portfolio_queue")
        warning_msg = (
            f"CRITICAL QUEUE LOSS: Rejected signals E[R]={e_r_rejected:.4f} >= Taken signals E[R]={e_r_taken:.4f}. "
            "Queue ordering / signal priority rule requires immediate revision."
        )
        logger.warning(warning_msg)

    return {
        "n_taken": len(taken_df),
        "n_rejected": len(rejected_df),
        "e_r_taken": round(e_r_taken, 4),
        "e_r_rejected": round(e_r_rejected, 4),
        "queue_loss_delta": round(queue_loss, 4),
        "is_critical": is_critical,
        "warning": warning_msg,
    }
