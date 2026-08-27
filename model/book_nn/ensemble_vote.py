"""Architecture ensemble voting (task T-25): FC + LSTM + MH Attention.

Each trained book-protocol model emits a regression prediction of the
(normalized) future return. ``model_probability`` squashes it through a
sigmoid so the vote operates on a [0, 1] "probability of up" scale - the
TradeLevel threshold (book default 0.6, NN book p. 688) then applies on the
ENSEMBLE mean, and a signal only fires when enough member models agree on
the direction (stability check).
"""
from __future__ import annotations

import numpy as np


def model_probability(prediction: np.ndarray) -> np.ndarray:
    """Map model output(s) to P(up).

    1-column output: sigmoid(column) - signed-return regression squashed to
    a probability. 2-column output (book's direction/strength pair): column
    0 is treated as the up-score and a softmax over the two columns is used.
    """
    pred = np.asarray(prediction, dtype=float)
    if pred.ndim == 1:
        pred = pred[:, None]
    if pred.shape[1] == 1:
        return 1.0 / (1.0 + np.exp(-pred[:, 0]))
    if pred.shape[1] == 2:
        z = pred - pred.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e[:, 0] / e.sum(axis=1)
    raise ValueError(f"unsupported prediction width {pred.shape[1]}")


def ensemble_vote(probabilities: dict[str, np.ndarray], trade_level: float = 0.6,
                  min_agreement: float = 0.6) -> dict:
    """Vote across model architectures.

    Parameters
    ----------
    probabilities : mapping model name -> P(up) array (one entry per sample)
    trade_level : signal threshold on the ensemble-mean probability
        (TradeLevel analog, book default 0.6).
    min_agreement : fraction of member models that must vote the same
        direction as the ensemble signal (stability of the signal, T-25).

    Returns a per-sample dict list with: mean probability, per-model votes,
    agreement fraction, and the final signal ("long" / "short" / "flat").
    """
    if not probabilities:
        raise ValueError("no models given")
    probs = {k: np.asarray(v, dtype=float) for k, v in probabilities.items()}
    names = list(probs)
    n_samples = len(probs[names[0]])
    for name in names:
        if len(probs[name]) != n_samples:
            raise ValueError("model probability arrays must align")

    mean_p = np.mean([probs[n] for n in names], axis=0)
    votes = {n: np.where(probs[n] >= trade_level, 1,
                         np.where(probs[n] <= 1.0 - trade_level, -1, 0))
             for n in names}
    results = []
    for i in range(n_samples):
        sample_votes = {n: int(votes[n][i]) for n in names}
        p = float(mean_p[i])
        if p >= trade_level:
            direction, direction_votes = 1, [v for v in sample_votes.values() if v == 1]
        elif p <= 1.0 - trade_level:
            direction, direction_votes = -1, [v for v in sample_votes.values() if v == -1]
        else:
            direction, direction_votes = 0, []
        agreement = (len(direction_votes) / len(names)) if direction else 0.0
        signal = {1: "long", -1: "short", 0: "flat"}[direction]
        if direction and agreement < min_agreement:
            signal = "flat"  # unstable: not enough members agree (T-25)
        results.append({
            "mean_probability": p,
            "model_votes": sample_votes,
            "agreement": agreement,
            "stability": agreement,      # alias: agreement == stability score
            "signal": signal,
        })
    return {"samples": results, "trade_level": trade_level,
            "min_agreement": min_agreement, "models": names}


def signal_stability(probabilities: dict[str, np.ndarray],
                     trade_level: float = 0.6) -> float:
    """Fraction of models whose vote matches the ensemble-mean direction."""
    out = ensemble_vote(probabilities, trade_level=trade_level, min_agreement=0.0)
    return float(np.mean([s["stability"] for s in out["samples"]]))
