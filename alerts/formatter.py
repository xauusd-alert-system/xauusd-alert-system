"""
Formats a signal JSON dict into a human-readable Telegram message with 3 TP levels.
"""
from datetime import datetime, timezone


def format_signal_message(signal: dict, asset_key: str = "XAUUSD") -> str:
    bias = signal["bias"]
    confidence_pct = round(signal.get("confidence", 0.0) * 100, 1)
    regime = signal.get("regime", "unknown")
    session = signal.get("session", "unknown")

    if bias == "no_trade":
        return (
            f"⚡ [{asset_key}] M5 Scalp Update\n"
            f"Bias: NO TRADE\n"
            f"Regime: {regime}\n"
            f"Session: {session}\n"
            f"Reason: {signal.get('reasoning_summary', 'Confidence below threshold')}"
        )

    direction_label = "🟢 LONG (BUY)" if bias == "long" else "🔴 SHORT (SELL)"
    entry_zone = signal.get("entry_zone")
    invalidation = signal.get("invalidation")
    targets = signal.get("targets") or []

    entry_str = f"{entry_zone[0]} - {entry_zone[1]}" if entry_zone else "N/A"
    
    tp1 = targets[0] if len(targets) > 0 else "N/A"
    tp2 = targets[1] if len(targets) > 1 else "N/A"
    tp3 = targets[2] if len(targets) > 2 else "N/A"

    return (
        f"🎯 [{asset_key}] M5 SCALP SIGNAL\n"
        f"Direction: {direction_label}\n"
        f"Confidence: {confidence_pct}%\n"
        f"Regime: {regime.upper()}\n"
        f"Session: {session.upper()}\n\n"
        f"📍 Entry Zone: {entry_str}\n"
        f"🛑 Stop Loss: {invalidation}\n\n"
        f"🎯 TP1 (50% + BE): {tp1}\n"
        f"🎯 TP2 (30% + Trail): {tp2}\n"
        f"🎯 TP3 (20% Runner): {tp3}\n\n"
        f"💡 Strategy: Automatic BE on TP1, Trailing Stop on TP2!"
    )