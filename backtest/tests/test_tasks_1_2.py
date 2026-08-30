import numpy as np
import pytest

from backtest.deflated_sharpe import (
    deflated_sharpe_ratio,
    effective_number_trials,
    n_eff_participation_ratio,
)
from model.cv import purged_kfold_indices
from model.uniqueness import (
    average_uniqueness_weights,
    compute_event_uniqueness,
)


def test_task1_uniqueness_t_eff_less_than_nominal_by_at_least_30_pct():
    """Unit test Task 1: on a synthetic set of overlapping labels/trades,
    T_eff is at least 30% lower than nominal N."""
    N = 100
    horizon = 20  # each trade/label overlaps 20 future bars
    w = average_uniqueness_weights(N, horizon)
    valid_w = w[w > 0]
    T_eff = float(np.sum(valid_w))
    nominal_N = float(len(valid_w))

    # T_eff must be < nominal_N * 0.70 (minimum 30% reduction)
    reduction = 1.0 - (T_eff / nominal_N)
    assert reduction >= 0.30, f"Uniqueness reduction {reduction:.1%} < 30%"
    assert T_eff < nominal_N * 0.70


def test_task1_event_uniqueness_spans():
    """Test compute_event_uniqueness on discrete overlapping intervals."""
    # 2 completely overlapping trades on [0, 10]
    spans_full_overlap = [(0, 10), (0, 10)]
    u_overlap = compute_event_uniqueness(spans_full_overlap)
    assert len(u_overlap) == 2
    assert np.allclose(u_overlap, [0.5, 0.5])
    assert float(np.sum(u_overlap)) == pytest.approx(1.0)  # T_eff = 1.0 vs N=2 (50% reduction)

    # 10 overlapping trades staggered by 1 bar with horizon 10
    spans_staggered = [(i, i + 10) for i in range(10)]
    u_staggered = compute_event_uniqueness(spans_staggered)
    t_eff = float(np.sum(u_staggered))
    assert t_eff < 10 * 0.70, f"T_eff {t_eff} not < 7.0 (30% reduction)"


def test_task1_dsr_recalculated_with_t_eff_is_more_conservative():
    """Regression test Task 1: DSR is strictly lower (more conservative)
    when calculated with T_eff instead of nominal len(trades)."""
    rng = np.random.default_rng(42)
    # Series with modest positive Sharpe so DSR is in interior (0.1, 0.9)
    pnls = rng.normal(0.04, 0.40, size=60)
    n_trades = len(pnls)
    t_eff = n_trades * 0.5  # 50% effective sample size

    dsr_nominal = deflated_sharpe_ratio(pnls, n_trials=50, t_eff=n_trades)
    dsr_effective = deflated_sharpe_ratio(pnls, n_trials=50, t_eff=t_eff)

    assert dsr_effective["t_eff"] == pytest.approx(t_eff)
    assert dsr_effective["dsr"] < dsr_nominal["dsr"]


def test_task1_embargo_buffer_after_test_fold():
    """Unit test Task 1: embargo drops post-test train rows."""
    folds = purged_kfold_indices(100, n_splits=5, horizon=6, embargo=5)
    for k, (train_idx, test_idx) in enumerate(folds):
        test_start, test_end = test_idx[0], test_idx[-1] + 1
        # No train row should be in [test_end, test_end + 5)
        post_test_embargo = set(range(test_end, min(100, test_end + 5)))
        assert not (set(train_idx) & post_test_embargo)


def test_task2_n_eff_participation_ratio_known_structure():
    """Unit test Task 2: verify n_eff_participation_ratio on known correlation matrices."""
    # 1. Orthogonal returns (M trials, T observations)
    M = 10
    T = 200
    rng = np.random.default_rng(42)
    orth_returns = rng.normal(0, 1, size=(M, T))
    pr_orth = n_eff_participation_ratio(orth_returns)
    assert 7.0 <= pr_orth <= 10.0

    # 2. Rank-1 / perfectly identical returns
    same_return = rng.normal(0, 1, size=(1, T))
    identical_returns = np.repeat(same_return, M, axis=0)
    pr_identical = n_eff_participation_ratio(identical_returns)
    assert pr_identical == pytest.approx(1.0, abs=1e-3)

    # 3. Two orthogonal blocks (size M/2 each)
    block1 = np.repeat(rng.normal(0, 1, size=(1, T)), M // 2, axis=0)
    block2 = np.repeat(rng.normal(0, 1, size=(1, T)), M // 2, axis=0)
    blocks = np.vstack([block1, block2])
    pr_blocks = n_eff_participation_ratio(blocks)
    assert pr_blocks == pytest.approx(2.0, abs=0.05)


def test_task2_n_eff_participation_ratio_729_configs_range():
    """Unit test Task 2: on 729 trials with ~25 latent strategy clusters (as in real grid search),
    participation ratio falls into expected 15-40 range (audit doc expectation ~25)."""
    n_trials = 729
    n_clusters = 25
    n_obs = 100
    rng = np.random.default_rng(123)

    # 25 latent factor return streams
    factors = rng.normal(0, 1, size=(n_clusters, n_obs))
    # 729 configs sampled from the 25 clusters with small intra-cluster noise
    cluster_ids = np.repeat(np.arange(n_clusters), int(np.ceil(n_trials / n_clusters)))[:n_trials]
    noise = rng.normal(0, 0.15, size=(n_trials, n_obs))
    M = factors[cluster_ids] + noise

    res = effective_number_trials(M)
    pr = n_eff_participation_ratio(M)

    assert 15.0 <= pr <= 40.0, f"PR {pr:.2f} not in expected [15, 40] range"
    assert res["participation_ratio"] == pytest.approx(pr)
    assert res["n_eff_combined"] >= pr
