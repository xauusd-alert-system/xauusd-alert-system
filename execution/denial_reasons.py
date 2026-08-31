"""Denial reasons for signal filtering — TOP-3 closest to passing.

Used by the MT5 trader pipeline to make ``no trade`` logs explain *why*
there is no signal (night vs thresholds). Pure, deterministic, no MT5 dep.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DenialReason:
    """One gate that blocked a signal.

    Attributes:
        code: short gate id, e.g. ``ml_prob``
        detail: human ``fact<threshold`` or ``fact>threshold`` fragment,
                e.g. ``0.42<0.55``
        margin: 0..1 (or >1 if passed, but for denials <1) — closeness to
                passing. Larger = closer to threshold. Used to sort top-N.
    """

    code: str
    detail: str
    margin: float

    def render(self) -> str:
        return f"{self.code}={self.detail}"


def top_reasons(reasons: list[DenialReason], n: int = 3) -> list[DenialReason]:
    """Return the *n* reasons with the largest ``margin`` (closest to passing)."""
    if not reasons:
        return []
    try:
        n_int = int(n)
    except Exception:
        n_int = 3
    if n_int <= 0:
        return []
    return sorted(reasons, key=lambda r: -float(r.margin))[:n_int]


def render_line(reasons: list[DenialReason], n: int = 3) -> str:
    """One-line ``reasons: a | b | c`` for the trader log.

    If *reasons* is empty, returns ``reasons: none``.
    """
    top = top_reasons(reasons, n)
    if not top:
        return "reasons: none"
    # spec says first element is prefixed with ``reasons: ``
    return "reasons: " + " | ".join(r.render() for r in top)
