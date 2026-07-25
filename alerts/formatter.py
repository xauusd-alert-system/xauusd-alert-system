"""
Formats a signal JSON dict into a human-readable Telegram message.
Pure formatting logic, no network calls, no side effects - kept separate from
telegram_bot.py so it is trivially unit-testable without mocking HTTP calls.
"""
from datetime import datetime, timezone


def format_signal_message(signal: dict) -> str:
    """
    signal is the dict returned by realtime/pipeline.py::generate_signal().
    Produces a clearly formatted, emoji-free (professional tone) message with
    bias, confidence, entry zone, invalidation, and target levels.
    """
    bias = signal["bias"]
    confidence_pct = round(signal["confidence"] * 100, 1)
    regime = signal.get("regime", "unknown")
    session = signal.get("session", "unknown")

    if bias == "no_trade":
        return (
            f"XAUUSD Signal Update\n"
            f"Bias: NO TRADE\n"
            f"Regime: {regime}\n"
            f"Session: {session}\n"
            f"Reason: {signal.get('reasoning_summary', 'Confidence below threshold')}"
        )

    direction_label = "LONG" if bias == "long" else "SHORT"
    entry_zone = signal.get("entry_zone")
    invalidation = signal.get("invalidation")
    targets = signal.get("targets") or []

    entry_zone_str = f"{entry_zone[0]} - {entry_zone[1]}" if entry_zone else "N/A"
    targets_str = ", ".join(str(t) for t in targets) if targets else "N/A"

    return (
        f"XAUUSD Signal Alert\n"
        f"Bias: {direction_label}\n"
        f"Confidence: {confidence_pct}%\n"
        f"Regime: {regime}\n"
        f"Session: {session}\n"
        f"Entry zone: {entry_zone_str}\n"
        f"Invalidation: {invalidation}\n"
        f"Target(s): {targets_str}\n"
        f"Reasoning: {signal.get('reasoning_summary', '')}"
    )
