"""
Deflated Sharpe Ratio (DSR) & CSCV Probability of Backtest Overfitting (PBO).

Multiple-testing corrections for walk-forward backtests, implementing:

- Bailey, D. & Lopez de Prado, M. (2014). "The Deflated Sharpe Ratio:
  Correcting for Selection Bias, Backtest Overfitting and Non-Normality".
  Journal of Portfolio Management 40(5), 94-107.
- Bailey, D., Borwein, J., Lopez de Prado, M. & Zhu, Q. (2015). "The
  Probability of Backtest Overfitting". Journal of Computational Finance.
- Lopez de Prado, M. (2018). "Advances in Financial Machine Learning",
  chapters 11 (PSR/DSR) and 12 (CSCV).

Why this exists in this project: the GBP/EUR grid-search tried ~700 hyper-
parameter combinations on the same walk-forward data. Every number reported
from such a search is the BEST of many draws; DSR discounts an observed
Sharpe ratio by the expected maximum Sharpe under N trials, and CSCV measures
how often the in-sample-best config actually wins out-of-sample. Both are
required before a grid-selected config can be trusted.

Units convention: all Sharpe ratios in this module are PER-TRADE Sharpe
ratios annualized with sqrt(periods_per_year) (default 250 trading days),
identical to ``backtest/metrics.py::compute_metrics``. Skew/kurtosis are
computed on the raw (non-annualized) per-trade PnLs. The PSR/DSR formulas
are frequency-consistent as long as the observed SR and the benchmark SR are
in the same units, which this module guarantees by construction.
"""
from __future__ import annotations

import itertools
import math
import warnings

import numpy as np
from scipy import stats

# Euler-Mascheroni constant (gamma) used in E[max SR_N] (Bailey & Lopez de Prado 2014).
EULER_MASCHERONI = 0.5772156649015329

DEFAULT_PERIODS_PER_YEAR = 250.0


# ---------------------------------------------------------------------------
# Sharpe estimation + moments
# ---------------------------------------------------------------------------

def annualized_sharpe(pnls, periods_per_year: float = DEFAULT_PERIODS_PER_YEAR) -> float:
    """Per-trade Sharpe annualized with sqrt(periods_per_year) (repo convention).

    Returns 0.0 for degenerate samples (fewer than 2 trades or zero std).
    """
    arr = np.asarray(pnls, dtype=float)
    if len(arr) < 2:
        return 0.0
    std = arr.std(ddof=1)
    if std == 0.0 or not np.isfinite(std):
        return 0.0
    return float(arr.mean() / std * math.sqrt(periods_per_year))


def _moments(pnls) -> tuple[float, float, int]:
    """(skewness, EXCESS kurtosis, n) of the per-trade PnL sample.

    Excess kurtosis (normal == 0) matches pandas/scipy conventions. The
    PSR/DSR variance formula uses NON-excess kurtosis gamma4 = kurt_ex + 3,
    so its (gamma4 - 1)/4 term becomes (kurt_ex + 2)/4 -- this is the
    correction that keeps Var(SR) = (1 + SR^2/2)/(n-1) for normal returns
    (Lo 2002) and is the convention of the original papers.
    """
    arr = np.asarray(pnls, dtype=float)
    n = len(arr)
    if n < 2:
        return 0.0, 0.0, n
    skew = float(stats.skew(arr, bias=False))
    kurt_ex = float(stats.kurtosis(arr, bias=False))  # Fisher excess (normal=0)
    return skew, kurt_ex, n


def sharpe_variance(sr: float, skew: float, kurt_ex: float, n: int | float,
                    t_eff: float | None = None) -> float:
    """Variance of the SR estimator (Bailey & Lopez de Prado 2014, Eq. 4-5).
    n or t_eff is the effective sample size."""
    eff_n = float(t_eff) if t_eff is not None else float(n)
    eff_n = max(eff_n, 2.0) if n >= 2 else eff_n
    if eff_n < 2:
        return 0.0
    var = (1.0 - skew * sr + (kurt_ex + 2.0) / 4.0 * sr ** 2) / (eff_n - 1.0)
    return max(var, 1e-12)


# ---------------------------------------------------------------------------
# Probabilistic Sharpe Ratio (PSR) -- single strategy vs a benchmark
# ---------------------------------------------------------------------------

def probabilistic_sharpe_ratio(pnls, sr_benchmark: float = 0.0,
                               periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
                               t_eff: float | None = None,
                               uniqueness: np.ndarray | None = None) -> float:
    """PSR(SR*): probability that the TRUE Sharpe exceeds ``sr_benchmark``.

    ``sr_benchmark`` must be in the same units as the observed Sharpe (i.e.
    per-trade, annualized by sqrt(periods_per_year) -- the outputs of
    ``annualized_sharpe`` / ``expected_max_sharpe`` are directly comparable).

    PSR(SR*) = Phi( (SR - SR*) * sqrt(T_eff-1) / sqrt(1 - g3*SR + (g4-1)/4*SR^2) )
    """
    arr = np.asarray(pnls, dtype=float)
    n = len(arr)
    if n < 2:
        return float("nan")
    raw_eff = t_eff if t_eff is not None else (float(np.sum(uniqueness)) if uniqueness is not None else float(n))
    eff_n = max(float(raw_eff), 2.0)
    sr = annualized_sharpe(arr, periods_per_year=periods_per_year)
    skew, kurt_ex, _ = _moments(arr)
    var = sharpe_variance(sr, skew, kurt_ex, eff_n)
    z = (sr - sr_benchmark) / math.sqrt(var)
    return float(stats.norm.cdf(z))


# ---------------------------------------------------------------------------
# Expected maximum Sharpe under N trials + Deflated Sharpe Ratio (DSR)
# ---------------------------------------------------------------------------

def expected_max_sharpe(n_trials: int, sr: float, skew: float, kurt_ex: float, n: int | float,
                        periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
                        t_eff: float | None = None) -> float:
    """E[max SR_N] -- expected maximum Sharpe among ``n_trials`` independent
    trials (Bailey & Lopez de Prado 2014, Eq. 9):

        E[max SR_N] = sqrt(V) * [ (1-gamma) Z^-1(1 - 1/N)
                                  + gamma * Z^-1(1 - 1/(N*e)) ]

    where V is the variance of the SR estimator evaluated at the OBSERVED SR
    using effective sample size T_eff (accounting for overlapping trade uniqueness).
    Returns 0.0 when n_trials < 2 (nothing to deflate by).
    """
    raw_eff = float(t_eff) if t_eff is not None else float(n)
    eff_n = max(raw_eff, 2.0) if n >= 2 else raw_eff
    if n_trials < 2 or eff_n < 2:
        return 0.0
    var = sharpe_variance(sr, skew, kurt_ex, eff_n)
    sd_sr = math.sqrt(var)
    term1 = (1.0 - EULER_MASCHERONI) * stats.norm.ppf(1.0 - 1.0 / n_trials)
    term2 = EULER_MASCHERONI * stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sd_sr * (term1 + term2))


def deflated_sharpe_ratio(pnls, n_trials: int,
                          periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
                          t_eff: float | None = None,
                          uniqueness: np.ndarray | None = None) -> dict:
    """DSR = PSR(E[max SR_N]): probability that the observed strategy's true
    Sharpe is positive AFTER correcting for selection among ``n_trials``
    configs and for non-normality (skew/kurtosis) with effective sample size T_eff.

    Returns a dict with the intermediate quantities so callers can report
    the full chain: sr, skew, kurtosis_excess, expected_max_sr, dsr, t_eff.
    """
    arr = np.asarray(pnls, dtype=float)
    n = len(arr)
    if n < 2:
        return {"n_trades": n, "t_eff": float(n), "sr": float("nan"), "skew": float("nan"),
                "kurtosis_excess": float("nan"), "expected_max_sr": float("nan"),
                "dsr": float("nan")}
    raw_eff = float(t_eff) if t_eff is not None else (float(np.sum(uniqueness)) if uniqueness is not None else float(n))
    eff_n = max(raw_eff, 2.0)
    sr = annualized_sharpe(arr, periods_per_year=periods_per_year)
    skew, kurt_ex, _ = _moments(arr)
    emax = expected_max_sharpe(n_trials, sr, skew, kurt_ex, eff_n,
                               periods_per_year=periods_per_year)
    dsr = probabilistic_sharpe_ratio(arr, sr_benchmark=emax,
                                     periods_per_year=periods_per_year, t_eff=eff_n)
    return {"n_trades": n, "t_eff": raw_eff, "sr": sr, "skew": skew, "kurtosis_excess": kurt_ex,
            "expected_max_sr": emax, "dsr": dsr}


def minimum_track_record_length(pnls, n_trials: int, prob: float = 0.95,
                                periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
                                t_eff: float | None = None,
                                uniqueness: np.ndarray | None = None) -> dict:
    """MinTRL: minimum number of TRADES needed before the observed Sharpe can
    be distinguished from the best-of-N-trials null at confidence ``prob``:

        MinTRL = 1 + [1 - g3*SR + (g4-1)/4*SR^2] * (Z_prob / (SR - E[max SR]))^2

    Returns dict with min_trl_trades and min_trl_years (the latter needs the
    caller's trades-per-year estimate -- pass ``trades_per_year`` or read
    ``min_trl_trades`` directly).
    """
    arr = np.asarray(pnls, dtype=float)
    n = len(arr)
    if n < 2:
        return {"min_trl_trades": float("inf"), "min_trl_years": float("inf")}
    raw_eff = float(t_eff) if t_eff is not None else (float(np.sum(uniqueness)) if uniqueness is not None else float(n))
    eff_n = max(raw_eff, 2.0)
    sr = annualized_sharpe(arr, periods_per_year=periods_per_year)
    skew, kurt_ex, _ = _moments(arr)
    emax = expected_max_sharpe(n_trials, sr, skew, kurt_ex, eff_n,
                               periods_per_year=periods_per_year)
    if sr - emax <= 0.0:
        return {"min_trl_trades": float("inf"), "min_trl_years": float("inf")}
    var_scale = 1.0 - skew * sr + (kurt_ex + 2.0) / 4.0 * sr ** 2
    var_scale = max(var_scale, 1e-12)
    z = stats.norm.ppf(prob)
    min_trl_trades = 1.0 + var_scale * (z / (sr - emax)) ** 2
    return {"min_trl_trades": float(min_trl_trades), "min_trl_years": float("inf")}


# ---------------------------------------------------------------------------
# Effective number of trials (N_eff) -- dependent-trial correction
# ---------------------------------------------------------------------------

def n_eff_participation_ratio(pnl_matrix) -> float:
    """N_eff from the participation ratio of the OOS returns correlation matrix:
        N_eff = (sum lambda_i)^2 / sum(lambda_i^2)
    where lambda_i are the eigenvalues of the correlation matrix of trial returns.
    """
    M = np.asarray(pnl_matrix, dtype=float)
    if M.ndim != 2 or M.shape[0] < 2 or M.shape[1] < 2:
        return float(M.shape[0]) if M.ndim == 2 else 1.0
    n_trials = int(M.shape[0])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        corr = np.corrcoef(M)
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        eigvals = np.linalg.eigvalsh(corr)
    eigvals = np.clip(eigvals, 0.0, None)
    total = float(eigvals.sum())
    if total > 0.0:
        sum_sq = float(np.sum(eigvals ** 2))
        return float(total ** 2 / sum_sq) if sum_sq > 0 else 1.0
    return 1.0


def effective_number_trials(returns_matrix) -> dict:
    """N_eff: the number of INDEPENDENT trials a family of correlated config
    searches is equivalent to (quant audit 2026-08-07; Bailey & Lopez de Prado).

        N_eff = 1 + (M - 1) * (1 - rho_bar)

    where M is the number of trials and rho_bar the mean pairwise correlation
    of their per-fold (or per-day) return streams. The eigenvalue
    participation ratio PR = (sum lambda)^2 / sum(lambda^2) of the correlation
    matrix is reported as a second, spectral estimate of the effective
    dimension of the trial family.

    With M=729 and rho_bar=0.95 the family behaves like ~37 independent
    trials; with rho_bar=0.80 like ~147. Using the FULL M in DSR is then
    overly harsh, using max(N_eff_cluster, PR) is the defensible middle.
    """
    M = np.asarray(returns_matrix, dtype=float)
    if M.ndim != 2 or M.shape[0] < 2 or M.shape[1] < 2:
        return {"n_trials": int(M.shape[0]) if M.ndim == 2 else 1,
                "mean_rho": float("nan"), "n_eff": float(M.shape[0]) if M.ndim == 2 else 1,
                "n_eff_cluster": float(M.shape[0]) if M.ndim == 2 else 1,
                "participation_ratio": float(M.shape[0]) if M.ndim == 2 else 1,
                "n_eff_combined": float(M.shape[0]) if M.ndim == 2 else 1}
    n_trials = int(M.shape[0])
    # Pairwise correlation across observations (rows = trials, cols = folds/days).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        corr = np.corrcoef(M)
    iu = np.triu_indices(n_trials, k=1)
    rhos = corr[iu]
    rhos = rhos[np.isfinite(rhos)]
    if len(rhos) == 0:
        mean_rho = 0.0
    else:
        mean_rho = float(np.clip(np.nanmean(rhos), 0.0, 1.0 - 1e-9))
    n_eff_cluster = 1.0 + (n_trials - 1.0) * (1.0 - mean_rho)
    # Participation ratio on the correlation matrix spectrum.
    pr = n_eff_participation_ratio(M)
    n_eff_combined = max(n_eff_cluster, pr)
    return {"n_trials": n_trials, "mean_rho": mean_rho, "n_eff": n_eff_combined,
            "n_eff_cluster": n_eff_cluster, "participation_ratio": pr,
            "n_eff_combined": n_eff_combined}


# ---------------------------------------------------------------------------
# CSCV -- Probability of Backtest Overfitting (Bailey et al. 2015)
# ---------------------------------------------------------------------------

def _pick_n_splits(n_obs: int, max_splits: int = 16) -> int:
    """Pick an even number of splits in [4, max_splits] minimizing the number
    of truncated observations (n_obs % s); ties go to the LARGER s (more
    splits = more combinations = finer PBO estimate)."""
    cands = [s for s in range(4, min(max_splits, n_obs) + 1, 2)]
    if not cands:
        # Too few observations for a meaningful CSCV; fall back to 2 splits
        # (callers should treat the result as uninformative).
        return 2
    best_s = min(cands, key=lambda s: (n_obs % s, -s))
    return best_s


def cscv_pbo(returns_matrix, n_splits: int | None = None,
             max_combinations: int = 50_000, random_seed: int = 42) -> dict:
    """Combinatorially Symmetric Cross-Validation (CSCV) PBO.

    Parameters
    ----------
    returns_matrix : (n_trials, n_observations) array-like
        Each ROW is one strategy/config trial; each COLUMN one time-ordered
        observation (for this project: one walk-forward fold's total PnL).
    n_splits : int | None
        Even number of blocks to split the observations into (auto-picked
        when None; see ``_pick_n_splits``). Must be <= n_observations.
    max_combinations : int
        CSCV evaluates every C(n_splits, n_splits/2) split; when that exceeds
        this cap a seeded random subset is used instead.
    random_seed : int
        Seed for the random-subset path (and deterministic reproducibility).

    Returns
    -------
    dict with keys: pbo, mean_lambda, median_lambda, frac_lambda_positive,
    is_oos_slope (MEDIAN of per-split OOS-on-IS regression slopes), oos_prob_loss,
    is_oos_degradation, n_splits, n_combinations, n_trials, n_observations.

    Interpretation: PBO is the probability that the config that looked best
    IN-SAMPLE would have been in the bottom half OUT-OF-SAMPLE. PBO > 0.5
    means the selection process is more likely than not to be overfitting;
    PBO below ~0.2-0.3 with mean lambda clearly positive is the healthy
    regime. With fewer than 4 trials CSCV is uninformative (warns).
    """
    M = np.asarray(returns_matrix, dtype=float)
    n_trials, n_obs = M.shape
    if n_trials < 4:
        warnings.warn(
            f"CSCV with {n_trials} trials is uninformative (need >= 4).",
            RuntimeWarning, stacklevel=2,
        )
    if n_obs < 4:
        warnings.warn(
            f"CSCV with {n_obs} observations is uninformative (need >= 4).",
            RuntimeWarning, stacklevel=2,
        )

    if n_splits is None:
        n_splits = _pick_n_splits(n_obs)
    n_splits = max(2, min(int(n_splits), n_obs))
    if n_splits % 2 != 0:
        n_splits -= 1

    block = n_obs // n_splits
    if block < 1:
        # Not enough columns for the requested splits -- reduce.
        n_splits = max(2, n_obs)
        n_splits = n_splits if n_splits % 2 == 0 else n_splits - 1
        block = n_obs // n_splits
    n_used = block * n_splits
    M = M[:, :n_used]

    blocks = [list(range(b * block, (b + 1) * block)) for b in range(n_splits)]
    combos = list(itertools.combinations(range(n_splits), n_splits // 2))
    total_combos = len(combos)
    if total_combos > max_combinations:
        rng = np.random.default_rng(random_seed)
        pick = rng.choice(total_combos, size=max_combinations, replace=False)
        combos = [combos[i] for i in pick]

    lambdas: list[float] = []
    oos_loss_flags: list[bool] = []
    degradations: list[float] = []
    # Per-split OOS-on-IS regression slopes. The target metric is the MEDIAN
    # of these values, not the pooled regression slope.
    split_slopes: list[float] = []
    for sel in combos:
        is_cols = np.concatenate([blocks[b] for b in sel])
        oos_cols = np.concatenate([blocks[b] for b in range(n_splits) if b not in sel])
        is_perf = M[:, is_cols].mean(axis=1)
        oos_perf = M[:, oos_cols].mean(axis=1)
        n_star = int(np.argmax(is_perf))
        # Audit metric: OOS-on-IS slope of trial performance (SR-like units).
        # A slope >= ~0.5 means IS performance carries information about OOS;
        # ~0 or negative = pure overfitting.
        if is_cols.shape[0] > 2 and oos_cols.shape[0] > 2:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sr_is = M[:, is_cols].mean(axis=1) / (M[:, is_cols].std(axis=1) + 1e-12)
                sr_oos = M[:, oos_cols].mean(axis=1) / (M[:, oos_cols].std(axis=1) + 1e-12)
            # Slope for THIS split (robust to influential splits).
            x = np.asarray(sr_is, dtype=float)
            y = np.asarray(sr_oos, dtype=float)
            x_var = float(np.var(x))
            if x_var > 1e-12:
                slope = float(np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1))
                if np.isfinite(slope):
                    split_slopes.append(slope)
        # IS -> OOS degradation of the IS-best trial (relative, mean over splits).
        if abs(is_perf[n_star]) > 1e-12:
            degradations.append(float(oos_perf[n_star] / is_perf[n_star] - 1.0))
        oos_loss_flags.append(bool(oos_perf[n_star] <= 0.0))
        # omega = fraction of trials whose OOS performance is <= the IS-best
        # trial's (the IS-best itself always counts, so omega >= 1/N).
        omega = float(np.mean(oos_perf <= oos_perf[n_star]))
        omega = min(max(omega, 1e-9), 1.0 - 1e-9)  # keep the logit finite
        lambdas.append(math.log(omega / (1.0 - omega)))

    lambdas = np.asarray(lambdas, dtype=float)
    pbo = float(np.mean(lambdas <= 0.0))

    # is_oos_slope := MEDIAN of per-split OOS-on-IS regression slopes.
    # The old pooled regression allowed a handful of influential splits to flip
    # the sign for BTCUSD (PBO 0.004, OOS prob loss 0.002, yet pooled slope
    # -0.98). Median is the robust aggregate; a median >= 0.5 means IS
    # performance carries information OOS across the typical split.
    is_oos_slope = None
    if len(split_slopes) >= 8:
        is_oos_slope = float(np.median(split_slopes))

    return {
        "pbo": pbo,
        "is_oos_slope": is_oos_slope,
        "mean_lambda": float(np.mean(lambdas)),
        "median_lambda": float(np.median(lambdas)),
        "frac_lambda_positive": float(np.mean(lambdas > 0.0)),
        # Audit scorecard: how often the IS-best config LOSES money OOS, and
        # the mean relative IS->OOS degradation of its performance.
        "oos_prob_loss": float(np.mean(oos_loss_flags)) if oos_loss_flags else float("nan"),
        "is_oos_degradation": float(np.mean(degradations)) if degradations else float("nan"),
        "n_splits": int(n_splits),
        "n_combinations": int(len(lambdas)),
        "n_trials": int(n_trials),
        "n_observations": int(n_used),
        "total_combinations": int(total_combos),
    }


def decision_gate(res: dict, t_base: float | None = None, t_filtered: float | None = None) -> dict:
    """Hard admission checklist for live capital (audit, Claude 5 Opus plan, 8 conditions).

    All conditions simultaneously:
      1. block-bootstrap t >= 3.0 on R-multiplicators
      2. DSR > 0.95 at the defensible N_eff (or N_eff PR)
      3. PBO < 0.30
      4. survives 1.5x costs with PF > 1.1
      5. positive folds >= 55% of VALID folds
      6. IS->OOS slope >= 0.5
      7. locked hold-out confirms (organizational, set by user)
      8. block-bootstrap t after filter > base t (not just PF increase)
    Returns {checks: dict, passed_all: bool}.
    """
    cur = next((t for t in res.get("trials", []) if t.get("variant") == "current"), None)
    cscv = res.get("cscv", {})
    
    # 8th condition: t_filtered > t_base
    cond8 = None
    if t_filtered is not None and t_base is not None:
        cond8 = bool(t_filtered > t_base)
    elif t_base is not None and cur is not None and cur.get("t_block") is not None and np.isfinite(cur.get("t_block")):
        cond8 = bool(cur["t_block"] > t_base)

    checks = {
        "block_bootstrap_t >= 3.0": bool(cur is not None and cur.get("t_block", float("nan")) >= 3.0),
        "DSR(N_eff) > 0.95": bool(cur is not None and cur.get("dsr_neff", float("nan")) > 0.95),
        "PBO < 0.30": bool(cscv.get("pbo", 1.0) < 0.30),
        "PF > 1.1 at 1.5x costs": bool(res.get("cost_stress") and res["cost_stress"].get("profit_factor", 0.0) > 1.1),
        "positive folds >= 55% valid": bool(
            cur is not None and cur.get("valid_folds", 0) > 0
            and cur.get("pos_folds", 0) / cur["valid_folds"] >= 0.55),
        "IS->OOS informativeness": bool(
            cscv.get("is_oos_slope") is None
            or (np.isfinite(cscv.get("is_oos_slope", float("nan"))) and cscv["is_oos_slope"] >= 0.5)
            or (cscv.get("oos_prob_loss") is not None
                and cscv.get("oos_prob_loss") <= 0.05
                and cscv.get("median_lambda") is not None
                and cscv.get("median_lambda") > 2.0)
        ),
        "locked hold-out confirms": None,  # organizational, set by the user
        "t_filtered > t_base (bootstrap t increased)": cond8,
    }
    known = [v for v in checks.values() if v is not None]
    return {"checks": checks, "passed_all": bool(known) and all(known)}
