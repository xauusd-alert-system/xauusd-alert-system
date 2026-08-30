"""Tests for scripts.subset_scan — the shared subset-scan tool with
Bonferroni/DSR multiple-testing correction used by diag scripts so edge
hunting is not done by hand (which invites noise mining).
"""

import numpy as np
import pandas as pd
import pytest

from scripts.subset_scan import (
    SubsetScanner,
    _bonferroni,
    _compute_subset_metrics,
    _t_to_p_two_sided,
)

# ---------------------------------------------------------------------------
# Statistical primitives
# ---------------------------------------------------------------------------


def test_bonferroni_basic():
    assert _bonferroni([0.01, 0.02, 0.03]) == [0.03, 0.06, 0.09]
    # Capped at 1.0, never exceeds it.
    assert _bonferroni([0.5, 0.4]) == [1.0, 0.8]
    assert _bonferroni([]) == []


def test_t_to_p_two_sided_symmetry():
    # Large |t| -> tiny p; t=0 -> p=1.
    assert _t_to_p_two_sided(0.0, 30) == 1.0
    assert _t_to_p_two_sided(10.0, 30) < 1e-9
    assert _t_to_p_two_sided(-10.0, 30) < 1e-9
    assert abs(_t_to_p_two_sided(2.0, 30) - _t_to_p_two_sided(-2.0, 30)) < 1e-12
    # Degenerate inputs are tolerated.
    assert _t_to_p_two_sided(float("nan"), 30) == 1.0
    assert _t_to_p_two_sided(1.0, 0) == 1.0


# ---------------------------------------------------------------------------
# Metrics + verdicts
# ---------------------------------------------------------------------------


def test_compute_metrics_all_positive_is_sig():
    r = np.full(60, 1.1)
    m = _compute_subset_metrics(r, "tp", min_trades=5)
    assert m.n == 60
    assert m.WR_pct == 100.0
    assert m.PF == 999.0  # no losses
    assert m.mean_R == pytest.approx(1.1)
    assert m.verdict == "pending"


def test_compute_metrics_all_negative_pf_zero():
    r = np.full(40, -1.0)
    m = _compute_subset_metrics(r, "stop", min_trades=5)
    assert m.WR_pct == 0.0
    assert m.PF == 0.0
    assert m.mean_R == pytest.approx(-1.0)


def test_compute_metrics_too_few():
    m = _compute_subset_metrics(np.array([0.5, 0.5]), "tiny", min_trades=5)
    assert m.verdict == "too_few"


def test_compute_metrics_empty():
    m = _compute_subset_metrics(np.array([]), "empty")
    assert m.verdict == "too_few"
    assert m.n == 0


# ---------------------------------------------------------------------------
# Scanner end-to-end
# ---------------------------------------------------------------------------


def _make_trades(n=200, seed=7):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {
            "regime": rng.choice(["trend_up", "trend_down", "range"], n),
            "direction": rng.choice([1, -1], n),
            "session": rng.choice(["london", "newyork", "asia"], n),
            "exit_reason": rng.choice(["tp", "stop", "be"], n),
            "R": rng.normal(0.0, 1.0, n),
        }
    )
    # Inject a strong real signal into one subset: newyork short tp trades.
    mask = (df["session"] == "newyork") & (df["direction"] == -1) & (df["exit_reason"] == "tp")
    df.loc[mask, "R"] = rng.normal(1.5, 0.4, int(mask.sum()))
    return df


def test_scan_finds_injected_signal():
    df = _make_trades()
    scanner = SubsetScanner(df, r_col="R", min_trades=5)
    scanner.add_groupby("session")
    scanner.add_groupby("direction", map_fn=lambda d: "long" if d == 1 else "short")
    scanner.add_groupby("exit_reason")
    results = scanner.scan()

    verdicts = {r.label: r.verdict for r in results}
    # The injected edge survives Bonferroni + DSR.
    sig_labels = [l for l, v in verdicts.items() if v == "sig"]
    assert any("newyork" in l and "short" in l and "tp" in l for l in sig_labels), sig_labels


def test_scan_marks_robust_losers_sig_neg():
    df = _make_trades()
    # Force one subset to be uniformly losing.
    mask = df["exit_reason"] == "stop"
    df.loc[mask, "R"] = -1.0
    scanner = SubsetScanner(df, r_col="R", min_trades=5)
    scanner.add_groupby("exit_reason")
    results = scanner.scan()
    verdicts = {r.label: r.verdict for r in results}
    assert verdicts.get("exit_reason=stop") == "sig_neg", verdicts


def test_scan_noise_stays_non_sig():
    rng = np.random.default_rng(42)
    df = pd.DataFrame(
        {
            "bucket": rng.choice(["a", "b", "c"], 300),
            "R": rng.normal(0.0, 1.0, 300),
        }
    )
    scanner = SubsetScanner(df, r_col="R", min_trades=5)
    scanner.add_groupby("bucket")
    results = scanner.scan()
    sig = [r for r in results if r.verdict in ("sig", "sig_neg")]
    assert not sig, [r.label for r in sig]


def test_scan_always_includes_all_baseline():
    df = _make_trades()
    scanner = SubsetScanner(df, r_col="R")
    scanner.add_groupby("session")
    results = scanner.scan()
    assert any(r.label == "ALL" for r in results)


def test_to_dict_shape():
    m = _compute_subset_metrics(np.full(20, 0.5), "x", min_trades=5)
    d = m.to_dict()
    for k in (
        "subset",
        "n",
        "mean_R",
        "sum_R",
        "WR%",
        "PF",
        "t_block",
        "sharpe_est",
        "p_raw",
        "p_bonf",
        "DSR",
        "verdict",
    ):
        assert k in d


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_smoke(tmp_path):
    from scripts.subset_scan import main

    df = _make_trades(120)
    csv = tmp_path / "trades.csv"
    out = tmp_path / "scan.csv"
    df.to_csv(csv, index=False)
    main(
        [
            "--csv",
            str(csv),
            "--r-col",
            "R",
            "--groupby",
            "session",
            "--groupby",
            "direction",
            "--out",
            str(out),
        ]
    )
    assert out.exists()
    out_df = pd.read_csv(out)
    assert "verdict" in out_df.columns
    assert len(out_df) >= 4  # ALL + session buckets + direction split
