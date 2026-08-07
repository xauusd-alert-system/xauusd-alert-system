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


def sharpe_variance(sr: float, skew: float, kurt_ex: float, n: int) -> float:
    """Variance of the SR estimator (Bailey & Lopez de Prado 2014, Eq. 4-5)."""
    if n < 2:
        return 0.0
    var = (1.0 - skew * sr + (kurt_ex + 2.0) / 4.0 * sr ** 2) / (n - 1.0)
    return max(var, 1e-12)


# ---------------------------------------------------------------------------
# Probabilistic Sharpe Ratio (PSR) -- single strategy vs a benchmark
# ---------------------------------------------------------------------------

def probabilistic_sharpe_ratio(pnls, sr_benchmark: float = 0.0,
                               periods_per_year: float = DEFAULT_PERIODS_PER_YEAR) -> float:
    """PSR(SR*): probability that the TRUE Sharpe exceeds ``sr_benchmark``.

    ``sr_benchmark`` must be in the same units as the observed Sharpe (i.e.
    per-trade, annualized by sqrt(periods_per_year) -- the outputs of
    ``annualized_sharpe`` / ``expected_max_sharpe`` are directly comparable).

    PSR(SR*) = Phi( (SR - SR*) * sqrt(n-1) / sqrt(1 - g3*SR + (g4-1)/4*SR^2) )
    """
    arr = np.asarray(pnls, dtype=float)
    n = len(arr)
    if n < 2:
        return float("nan")
    sr = annualized_sharpe(arr, periods_per_year=periods_per_year)
    skew, kurt_ex, _ = _moments(arr)
    var = sharpe_variance(sr, skew, kurt_ex, n)
    z = (sr - sr_benchmark) / math.sqrt(var)
    return float(stats.norm.cdf(z))


# ---------------------------------------------------------------------------
# Expected maximum Sharpe under N trials + Deflated Sharpe Ratio (DSR)
# ---------------------------------------------------------------------------

def expected_max_sharpe(n_trials: int, sr: float, skew: float, kurt_ex: float, n: int,
                        periods_per_year: float = DEFAULT_PERIODS_PER_YEAR) -> float:
    """E[max SR_N] -- expected maximum Sharpe among ``n_trials`` independent
    trials (Bailey & Lopez de Prado 2014, Eq. 9):

        E[max SR_N] = sqrt(V) * [ (1-gamma) Z^-1(1 - 1/N)
                                  + gamma * Z^-1(1 - 1/(N*e)) ]

    where V is the variance of the SR estimator evaluated at the OBSERVED SR.
    Returns 0.0 when n_trials < 2 (nothing to deflate by).
    """
    if n_trials < 2 or n < 2:
        return 0.0
    var = sharpe_variance(sr, skew, kurt_ex, n)
    sd_sr = math.sqrt(var)
    term1 = (1.0 - EULER_MASCHERONI) * stats.norm.ppf(1.0 - 1.0 / n_trials)
    term2 = EULER_MASCHERONI * stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sd_sr * (term1 + term2))


def deflated_sharpe_ratio(pnls, n_trials: int,
                          periods_per_year: float = DEFAULT_PERIODS_PER_YEAR) -> dict:
    """DSR = PSR(E[max SR_N]): probability that the observed strategy's true
    Sharpe is positive AFTER correcting for selection among ``n_trials``
    configs and for non-normality (skew/kurtosis).

    Returns a dict with the intermediate quantities so callers can report
    the full chain: sr, skew, kurtosis_excess, expected_max_sr, dsr.
    """
    arr = np.asarray(pnls, dtype=float)
    n = len(arr)
    if n < 2:
        return {"n_trades": n, "sr": float("nan"), "skew": float("nan"),
                "kurtosis_excess": float("nan"), "expected_max_sr": float("nan"),
                "dsr": float("nan")}
    sr = annualized_sharpe(arr, periods_per_year=periods_per_year)
    skew, kurt_ex, _ = _moments(arr)
    emax = expected_max_sharpe(n_trials, sr, skew, kurt_ex, n,
                               periods_per_year=periods_per_year)
    dsr = probabilistic_sharpe_ratio(arr, sr_benchmark=emax,
                                     periods_per_year=periods_per_year)
    return {"n_trades": n, "sr": sr, "skew": skew, "kurtosis_excess": kurt_ex,
            "expected_max_sr": emax, "dsr": dsr}


def minimum_track_record_length(pnls, n_trials: int, prob: float = 0.95,
                                periods_per_year: float = DEFAULT_PERIODS_PER_YEAR) -> dict:
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
    sr = annualized_sharpe(arr, periods_per_year=periods_per_year)
    skew, kurt_ex, _ = _moments(arr)
    emax = expected_max_sharpe(n_trials, sr, skew, kurt_ex, n,
                               periods_per_year=periods_per_year)
    if sr - emax <= 0.0:
        return {"min_trl_trades": float("inf"), "min_trl_years": float("inf")}
    var_scale = 1.0 - skew * sr + (kurt_ex + 2.0) / 4.0 * sr ** 2
    var_scale = max(var_scale, 1e-12)
    z = stats.norm.ppf(prob)
    min_trl_trades = 1.0 + var_scale * (z / (sr - emax)) ** 2
    return {"min_trl_trades": float(min_trl_trades), "min_trl_years": float("inf")}


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
    dict with keys: pbo (fraction of splits where the IS-best trial is in the
    bottom half OOS), mean_lambda, median_lambda, frac_lambda_positive,
    n_splits, n_combinations, n_trials, n_observations.

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
    for sel in combos:
        is_cols = np.concatenate([blocks[b] for b in sel])
        oos_cols = np.concatenate([blocks[b] for b in range(n_splits) if b not in sel])
        is_perf = M[:, is_cols].mean(axis=1)
        oos_perf = M[:, oos_cols].mean(axis=1)
        n_star = int(np.argmax(is_perf))
        # omega = fraction of trials whose OOS performance is <= the IS-best
        # trial's (the IS-best itself always counts, so omega >= 1/N).
        # omega == 1 means the IS-best is ALSO the OOS best -> lambda = +inf
        # -> the split votes "not overfit". omega <= 0.5 means the IS-best
        # landed in the bottom half OOS -> the split votes "overfit".
        omega = float(np.mean(oos_perf <= oos_perf[n_star]))
        omega = min(max(omega, 1e-9), 1.0 - 1e-9)  # keep the logit finite
        lambdas.append(math.log(omega / (1.0 - omega)))

    lambdas = np.asarray(lambdas, dtype=float)
    pbo = float(np.mean(lambdas <= 0.0))
    return {
        "pbo": pbo,
        "mean_lambda": float(np.mean(lambdas)),
        "median_lambda": float(np.median(lambdas)),
        "frac_lambda_positive": float(np.mean(lambdas > 0.0)),
        "n_splits": int(n_splits),
        "n_combinations": int(len(lambdas)),
        "n_trials": int(n_trials),
        "n_observations": int(n_used),
        "total_combinations": int(total_combos),
    }
