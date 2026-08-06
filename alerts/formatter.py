"""
Clean signal formatter for Telegram alerts.

Implements the equal-step TP/SL grid specification:

    step   = signal["step"] (dynamic 1.0 * ATR) or signal["atr"];
              if absent, step is derived from the signal targets / invalidation
    TP1    = entry ± 1 * step
    TP2    = entry ± 2 * step   (exactly 2x the TP1 distance)
    TP3    = entry ± 3 * step   (exactly 3x the TP1 distance)
    Stop   = entry ∓ 3 * step   (exactly 3x the step => risk:TP3 = 1:1)

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
    return {
        "entry": entry,
        "step": step,
        "tp1": entry + direction * step,
        "tp2": entry + direction * 2.0 * step,
        "tp3": entry + direction * 3.0 * step,
        "sl": entry - direction * 3.0 * step,
    }


def format_clean_signal_message(signal: dict, asset_key: str = "XAUUSD") -> str:
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

    return (
        f"{direction}\n"
        f"{asset_line}\n"
        f"Зона входа: {_fmt_price(levels['entry'])}\n"
        f"Цели:\n"
        f"→ TP1: {_fmt_price(levels['tp1'])}\n"
        f"→ TP2: {_fmt_price(levels['tp2'])}\n"
        f"→ TP3: {_fmt_price(levels['tp3'])}\n"
        f"Стоп: {_fmt_price(levels['sl'])}"
    )


def format_signal_message(signal: dict, asset_key: str = "XAUUSD") -> str:
    """Backwards-compatible entry point; emits the clean signal format."""
    return format_clean_signal_message(signal, asset_key)
