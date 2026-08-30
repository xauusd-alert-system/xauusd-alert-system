"""
Clean signal formatter for Telegram alerts.

Implements the versioned SignalSpec display contract. Explicit target legs and
invalidation are authoritative and may contain TP4 or other validated legs.
Legacy signals without target_legs fall back to the historical equal-step 1/2/3
layout for backwards-compatible rendering.

TradeGroupSpec v1 path (ТЗ §19/§20/§21): when a signal carries
``schema_version == "trade-group.v1"`` (or an embedded ``group_spec``), the
final geometry is AUTHORITATIVE — the formatter never recomputes ATR/step/TP/SL.
Missing final geometry on the v1 path is a ``formatter_error``, not a reason to
build new levels. Legacy fallback exists ONLY for old signals.

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

import warnings
from datetime import UTC
from typing import Optional

from execution.trade_group import GROUP_SCHEMA_VERSION, TradeGroupSpec

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
        "Cannot resolve TP/SL step: signal must provide 'step', 'atr', equal-step 'targets' or 'invalidation'"
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


def format_clean_signal_message(signal: dict, asset_key: str = "XAUUSD", include_meta: bool = False) -> str:
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

    For ``schema_version == "trade-group.v1"`` (or an embedded ``group_spec``)
    the final geometry is authoritative and NO recomputation happens (ТЗ §19).

    P2-21 (ТЗ Часть 7 п.7.2): the legacy recomputation path below is
    DEPRECATED. Every new signal must be converted to a TradeGroupSpec via
    ``build_trade_group_from_signal`` before formatting; a legacy signal that
    cannot be converted must not be sent (log ``formatter_error`` instead).
    The legacy branch emits a ``DeprecationWarning`` and exists ONLY for
    in-flight legacy signals created before the trade-group pipeline; its
    removal plan is tracked in docs/TODO.md.
    """
    group_payload = signal.get("group_spec") if isinstance(signal, dict) else None
    if group_payload is None and isinstance(signal, dict) and signal.get("schema_version") == GROUP_SCHEMA_VERSION:
        group_payload = signal
    if group_payload is not None:
        return format_trade_group_message(group_payload)

    _warn_legacy_formatting(signal)
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

    target_lines = "\n".join(f"→ TP{i}: {_fmt_price(price)}" for i, price in enumerate(levels["targets"], 1))
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
        message += f"\n\n📊 Conf: {confidence_pct}% · Regime: {regime} · Session: {session}"

    return message


def _warn_legacy_formatting(signal: dict) -> None:
    """P2-21: the legacy recomputation path is deprecated — every new signal
    must go through ``build_trade_group_from_signal`` / trade-group.v1."""
    warnings.warn(
        "alerts.formatter: legacy signal formatting (level recomputation) is "
        "deprecated; convert signals to trade-group.v1 via "
        "build_trade_group_from_signal (P2-21 / TZ 7.2)",
        DeprecationWarning,
        stacklevel=3,
    )


def format_signal_message(signal: dict, asset_key: str = "XAUUSD", include_meta: bool = False) -> str:
    """Backwards-compatible entry point; emits the clean signal format."""
    return format_clean_signal_message(signal, asset_key, include_meta=include_meta)


# --------------------------------------------------------------------------
# TradeGroupSpec v1 — authoritative final geometry (ТЗ §19–§22)
# --------------------------------------------------------------------------


def geometry_from_spec(spec: TradeGroupSpec) -> dict:
    """Parity helper: the one authoritative geometry dict used by Telegram, MT5
    request building and ledger payloads (ТЗ §20)."""
    return spec.as_geometry_payload()


def _require_final_geometry(spec: TradeGroupSpec) -> None:
    """ТЗ §19: for trade-group.v1 the final geometry is mandatory; a missing
    level is ``formatter_error``, never a recomputation."""
    missing = [
        name
        for name, value in (
            ("tp1", spec.geometry.tp1),
            ("tp2", spec.geometry.tp2),
            ("tp3", spec.geometry.tp3),
            ("sl", spec.geometry.sl),
            ("entry.reference", spec.entry.reference),
        )
        if value is None
    ]
    if missing:
        raise ValueError(f"formatter_error: trade-group.v1 requires final geometry; missing {missing}")


def _coerce_group_spec(spec: TradeGroupSpec | dict) -> TradeGroupSpec:
    if isinstance(spec, TradeGroupSpec):
        return spec
    if not isinstance(spec, dict):
        raise ValueError("formatter_error: expected TradeGroupSpec or dict")
    if spec.get("schema_version") != GROUP_SCHEMA_VERSION:
        raise ValueError(f"formatter_error: unsupported schema {spec.get('schema_version')!r}")
    try:
        return TradeGroupSpec.model_validate(spec)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ValueError(f"formatter_error: invalid trade-group.v1 payload: {exc}") from exc


def format_trade_group_message(spec: TradeGroupSpec | dict) -> str:
    """Telegram message for a validated TradeGroupSpec (ТЗ §21). Never computes
    ATR/step/TP/SL — the spec's final geometry is the only source.

    The levels come from ``spec.as_geometry_payload()`` — the SAME parity dict
    used by paper execution and ledger payloads (follow-up ТЗ §15): there is no
    independent level calculation in the Telegram layer."""
    spec = _coerce_group_spec(spec)
    _require_final_geometry(spec)
    levels = spec.as_geometry_payload()

    emoji = "🟢" if spec.side == "long" else "🔴"
    direction = "ЛОНГ" if spec.side == "long" else "ШОРТ"
    zone_low = _fmt_price(spec.entry.low)
    zone_high = _fmt_price(spec.entry.high)
    allocation_by_leg = {t.leg: t.allocation for t in spec.targets}
    # floor-based percentages so the three lines always sum to 100.00
    # (0.333333/0.333333/0.333334 -> 33.33% / 33.33% / 33.34%)
    pct1 = int(allocation_by_leg[1] * 10000) / 100
    pct2 = int(allocation_by_leg[2] * 10000) / 100
    pct3 = round(100.0 - pct1 - pct2, 2)

    lines = [
        f"{emoji} {direction} · {spec.asset_key}",
        f"Режим: {spec.mode.upper()}",
        f"Group: {spec.group_id}",
        "",
        f"Зона входа: {zone_low} — {zone_high}",
        f"Стоп: {_fmt_price(levels['sl'])}",
        "",
        f"TP1: {_fmt_price(levels['tp1'])} · {pct1:.2f}%",
        f"TP2: {_fmt_price(levels['tp2'])} · {pct2:.2f}%",
        f"TP3: {_fmt_price(levels['tp3'])} · {pct3:.2f}%",
        "",
        "После TP1:",
        "SL остатка → BE + cost buffer",
    ]
    if spec.expires_at_utc_ms:
        from datetime import datetime

        expires = datetime.fromtimestamp(spec.expires_at_utc_ms / 1000, tz=UTC)
        lines.append(f"Срок идеи: {expires.strftime('%H:%M')} UTC")
    lines.append(f"Profile: {spec.profile_id}")
    return "\n".join(lines)


def format_group_lifecycle_update(
    *,
    group_id: str,
    event_type: str,
    state: str,
    remaining_legs: Optional[int] = None,
    sl_price: Optional[float] = None,
    timestamp_utc: Optional[str] = None,
    extra: Optional[str] = None,
) -> str:
    """Lifecycle update message (ТЗ §22): every update carries groupId, event
    type, confirmed state and timestamp; no false-positive claims."""
    lines = [f"{event_type}\nGroup: {group_id}"]
    if remaining_legs is not None:
        lines.append(f"Remaining legs: {remaining_legs}")
    if sl_price is not None:
        lines.append(f"SL remaining legs: {_fmt_price(sl_price)}")
    if extra:
        lines.append(extra)
    lines.append(f"State: {state}")
    lines.append(f"Timestamp: {timestamp_utc or '(unset)'} UTC")
    return "\n".join(lines)
