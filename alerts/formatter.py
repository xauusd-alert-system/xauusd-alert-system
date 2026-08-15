"""
Clean signal formatter for Telegram alerts.

Implements the versioned SignalSpec display contract. Explicit target legs and
invalidation are authoritative and may contain TP4 or other validated legs.
Legacy signals without target_legs fall back to the historical equal-step 1/2/3
layout for backwards-compatible rendering.

Message layout (clean format):

    ШОРТ
    GOLD | ЗОЛОТО | XAUUSD
    Зона входа: 4255.66
    Цели:
    → TP1: 4251.4
    → TP2: 4247.14
    → TP3: 4242.89
    Стоп: 4268.42
"""
from typing import Optional

ASSET_LABELS = {
    "XAUUSD": "GOLD | ЗОЛОТО | XAUUSD",
    "XAGUSD": "SILVER | СЕРЕБРО | XAGUSD",
    "BTCUSD": "BITCOIN | БИТКОИН | BTCUSD",
}


def _fmt_price(value: float) -> str:
    """2-decimal price, trailing zeros stripped (4251.40 -> '4251.4')."""
    return f"{round(float(value), 2):.2f}".rstrip("0").rstrip(".")


def entry_price(signal: dict) -> float:
    """Entry = midpoint of the entry zone (falls back to a flat entry field)."""
    entry_zone = signal.get("entry_zone")
    if entry_zone:
        return (float(entry_zone[0]) + float(entry_zone[1])) / 2.0
    return float(signal["entry"])


def resolve_step(signal: dict) -> float:
    """
    Resolves the equal-step grid step.

    Priority: explicit "step" -> "atr" (dynamic 1.0 * ATR) -> derived from the
    signal's own equal-step targets -> derived from invalidation (|SL - entry| / 3).
    """
    for key in ("step", "atr"):
        value = signal.get(key)
        if value is not None:
            value = float(value)
            if value > 0:
                return value

    entry = entry_price(signal)
    targets = signal.get("targets") or []
    if len(targets) >= 2:
        step = abs(float(targets[1]) - float(targets[0]))
        if step > 0:
            return step
    if len(targets) == 1:
        step = abs(float(targets[0]) - entry)
        if step > 0:
            return step
    invalidation = signal.get("invalidation")
    if invalidation is not None:
        step = abs(float(invalidation) - entry) / 3.0
        if step > 0:
            return step

    raise ValueError(
        "Cannot resolve TP/SL step: signal must provide 'step', 'atr', "
        "equal-step 'targets' or 'invalidation'"
    )


def compute_levels(signal: dict, step: Optional[float] = None) -> dict:
    """
    Builds the equal-step trade grid for a signal.

    Long:  TP1/2/3 = entry + 1/2/3*step, Stop = entry - 3*step
    Short: TP1/2/3 = entry - 1/2/3*step, Stop = entry + 3*step
    """
    entry = entry_price(signal)
    if step is None:
        step = resolve_step(signal)
    step = float(step)
    direction = 1.0 if signal["bias"] == "long" else -1.0
    legs = signal.get("target_legs") or []
    supplied = [float(leg["price"]) for leg in legs if leg.get("price") is not None]
    targets = supplied or [entry + direction * step * n for n in (1.0, 2.0, 3.0)]
    stop = signal.get("invalidation") if supplied else None
    if stop is None:
        stop = entry - direction * 3.0 * step
    out = {"entry": entry, "step": step, "targets": targets, "sl": float(stop)}
    for i, price in enumerate(targets[:3], 1):
        out[f"tp{i}"] = price  # compatibility for existing consumers
    return out


def format_clean_signal_message(
    signal: dict, asset_key: str = "XAUUSD", include_meta: bool = False
) -> str:
    """
    Formats a trade signal in the clean Telegram layout:

        ШОРТ
        GOLD | ЗОЛОТО | XAUUSD
        Зона входа: 4255.66
        Цели:
        → TP1: 4251.4
        → TP2: 4247.14
        → TP3: 4242.89
        Стоп: 4268.42

    When include_meta=True a compact metadata footer is appended
    (Conf / Regime / Session) for auditability.
    """
    bias = signal["bias"]
    if bias == "no_trade":
        regime = signal.get("regime", "unknown")
        session = signal.get("session", "unknown")
        return (
            f"⚡ [{asset_key}] M5 Scalp Update\n"
            f"Bias: NO TRADE\n"
            f"Regime: {regime}\n"
            f"Session: {session}\n"
            f"Reason: {signal.get('reasoning_summary', 'Confidence below threshold')}"
        )

    direction = "ЛОНГ" if bias == "long" else "ШОРТ"
    asset_line = ASSET_LABELS.get(asset_key, asset_key)
    levels = compute_levels(signal)

    target_lines = "\n".join(
        f"→ TP{i}: {_fmt_price(price)}" for i, price in enumerate(levels["targets"], 1)
    )
    message = (
        f"{direction}\n"
        f"{asset_line}\n"
        f"Зона входа: {_fmt_price(levels['entry'])}\n"
        f"Цели:\n{target_lines}\n"
        f"Стоп: {_fmt_price(levels['sl'])}"
    )

    if include_meta:
        confidence_pct = round(signal.get("confidence", 0.0) * 100, 1)
        regime = signal.get("regime", "unknown")
        session = signal.get("session", "unknown")
        message += (
            f"\n\n📊 Conf: {confidence_pct}% · Regime: {regime} · Session: {session}"
        )

    return message


def format_signal_message(
    signal: dict, asset_key: str = "XAUUSD", include_meta: bool = False
) -> str:
    """Backwards-compatible entry point; emits the clean signal format."""
    return format_clean_signal_message(signal, asset_key, include_meta=include_meta)
