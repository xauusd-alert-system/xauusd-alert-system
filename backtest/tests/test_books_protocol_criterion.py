"""TZ_BOOKS T-03 + T-08: acceptance protocol and the OnTester criterion."""
from __future__ import annotations

import math

from backtest.tester_criterion import (
    on_tester_score,
    score_from_tester_statistics,
    score_from_trades,
)
from backtest.validation_protocol import (
    DEFAULT_PROTOCOL,
    evaluate_model_acceptance,
    forward_metrics,
    protocol_checklist,
)


# --------------------------------------------------------------------- T-03
def test_forward_metrics_basic():
    m = forward_metrics([10.0, -5.0, 10.0, -5.0, 10.0])
    assert m["trades"] == 5
    assert m["win_rate"] == 0.6
    assert math.isclose(m["profit_factor"], 30.0 / 10.0)
    assert m["net"] == 20.0


def test_forward_metrics_empty_and_degenerate():
    empty = forward_metrics([])
    assert empty["trades"] == 0
    assert empty["profit_factor"] == 0.0
    only_wins = forward_metrics([5.0, 5.0])
    assert only_wins["profit_factor"] == float("inf")
    only_losses = forward_metrics([-5.0, -5.0])
    assert only_losses["profit_factor"] == 0.0


def _accepted_metrics():
    # 60 trades, PF ~1.5, win rate 0.583 (the book's forward result)
    pnl = [12.0] * 35 + [-8.0] * 25
    return forward_metrics(pnl)


def test_acceptance_passes_book_forward_result():
    decision = evaluate_model_acceptance(
        _accepted_metrics(),
        signal_threshold_used=0.6,
        forward_days=370,
        params_frozen=True,
    )
    assert decision.accepted
    # on acceptance the reasons list carries the positive summary line
    assert decision.reasons and "accepted" in decision.reasons[0]
    assert all(decision.checks.values())


def test_acceptance_rejects_on_each_threshold():
    m = _accepted_metrics()

    too_few = dict(m, trades=10)
    assert not evaluate_model_acceptance(too_few)
    low_pf = dict(m, profit_factor=1.05)
    assert not evaluate_model_acceptance(low_pf)
    low_wr = dict(m, win_rate=0.50)
    assert not evaluate_model_acceptance(low_wr)
    low_threshold = evaluate_model_acceptance(m, signal_threshold_used=0.55)
    assert not low_threshold
    assert low_threshold.checks["signal_threshold"] is False
    short_forward = evaluate_model_acceptance(m, forward_days=90)
    assert not short_forward
    refit = evaluate_model_acceptance(m, params_frozen=False)
    assert not refit


def test_acceptance_reasons_are_explcit():
    decision = evaluate_model_acceptance(
        {"trades": 3, "profit_factor": 0.9, "win_rate": 0.4},
        forward_days=10, params_frozen=False)
    assert not decision.accepted
    assert len(decision.reasons) >= 3
    text = " ".join(decision.reasons)
    assert "trades" in text and "profit factor" in text


def test_protocol_defaults_match_the_book():
    assert DEFAULT_PROTOCOL["min_profit_factor"] == 1.2
    assert DEFAULT_PROTOCOL["min_win_rate"] == 0.55
    assert DEFAULT_PROTOCOL["min_signal_threshold"] == 0.6
    assert DEFAULT_PROTOCOL["min_trades"] == 30
    assert DEFAULT_PROTOCOL["forward_min_days"] == 365
    assert DEFAULT_PROTOCOL["tick_mode"] == "real_ticks"
    assert DEFAULT_PROTOCOL["split_ratios"] == (0.6, 0.2, 0.2)


def test_protocol_checklist_mentions_every_regimen_item():
    checklist = " ".join(protocol_checklist()).lower()
    for keyword in ("60/20/20", "real ticks", "frozen", "forward"):
        assert keyword in checklist, keyword


# --------------------------------------------------------------------- T-08
def test_score_formula_is_pf_sqrt_trades_minus_dd_weight():
    # PF 2.0, 100 trades, DD 10%, weight 0.25
    expected = 2.0 * math.sqrt(100) - 0.25 * 10.0
    assert math.isclose(
        on_tester_score(2.0, 100, 10.0, dd_penalty_weight=0.25), expected)


def test_score_rewards_trade_count_at_equal_pf():
    small = on_tester_score(1.5, 36, 5.0)
    larger = on_tester_score(1.5, 400, 5.0)
    assert larger > small


def test_score_penalizes_drawdown():
    light = on_tester_score(1.5, 100, 5.0)
    heavy = on_tester_score(1.5, 100, 40.0)
    assert light > heavy


def test_score_caps_infinite_pf():
    inf_score = on_tester_score(float("inf"), 100, 0.0, pf_cap=10.0)
    assert math.isclose(inf_score, 10.0 * math.sqrt(100))


def test_score_is_neg_inf_without_trades():
    assert on_tester_score(3.0, 0, 0.0) == float("-inf")
    assert on_tester_score(3.0, 5, 0.0, min_trades=30) == float("-inf")


def test_score_from_tester_statistics_mapping():
    stats = {"STAT_PROFIT_FACTOR": 1.6, "STAT_TRADES": 49,
             "STAT_EQUITY_DDRELATIVE": 8.0}
    expected = 1.6 * math.sqrt(49) - 8.0
    assert math.isclose(score_from_tester_statistics(stats), expected)


def test_score_from_trades_uses_equity_drawdown():
    pnl = [10.0] * 20 + [-30.0] + [10.0] * 10
    equity = []
    running = 0.0
    for p in pnl:
        running += p
        equity.append(running)
    score = score_from_trades(pnl, equity_curve=equity)
    # peak 200, trough 170 -> DD 15%
    expected_pf = 300.0 / 30.0
    assert math.isclose(score, expected_pf * math.sqrt(31) - 15.0)
