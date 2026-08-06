"""
Institutional & Smart Money Concepts (SMC) Microstructure Metrics.
Calculates strictly causal institutional indicators:
1. Manipulation Index (1-10) - stop hunts, fakeouts, absorption.
2. Zone Strength (0-100%) - durability and liquidity depth of current S/R or Order Block.
3. SMF Ratio (Smart Money Flow Ratio) - institutional volume flow vs retail churn.
4. Liquidity Grab Score (1-10) - sweeps of swing highs/lows, PDH/PDL, session liquidity.
5. Delta Confidence (LOW/MEDIUM/HIGH/VERY HIGH) - multi-timeframe order flow delta alignment.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List


def calculate_manipulation_index(df: pd.DataFrame, window: int = 20) -> tuple[int, str]:
    """
    Computes Manipulation Index on scale 1 to 10:
    - High wick ratios (long rejection shadows).
    - False breakout rate (closing back inside range after piercing local extremes).
    - Price-volume absorption (heavy volume with narrow bar spread).
    """
    if len(df) < 5:
        return 5, "умеренный уровень манипуляций в пределах нормы."

    slice_df = df.tail(window).copy()
    
    # 1. Wick ratio
    hl_range = (slice_df["high"] - slice_df["low"]).replace(0, np.nan)
    body = (slice_df["close"] - slice_df["open"]).abs()
    wick_ratio = 1.0 - (body / hl_range).fillna(0.5)
    mean_wick = wick_ratio.mean()

    # 2. Volume-price divergence / absorption
    vol = slice_df["volume"] if "volume" in slice_df.columns else pd.Series(1.0, index=slice_df.index)
    vol_mean = vol.mean() if vol.mean() > 0 else 1.0
    rel_vol = vol / vol_mean
    absorption_bars = ((rel_vol > 1.3) & (wick_ratio > 0.6)).sum()

    # 3. False breakouts of local swing high/low
    high_20 = slice_df["high"].rolling(10, min_periods=1).max().shift(1)
    low_20 = slice_df["low"].rolling(10, min_periods=1).min().shift(1)
    swept_high = (slice_df["high"] > high_20) & (slice_df["close"] < high_20)
    swept_low = (slice_df["low"] < low_20) & (slice_df["close"] > low_20)
    fakeout_count = (swept_high | swept_low).sum()

    # Score calculation (1 to 10)
    raw_score = (
        (mean_wick * 4.0) +
        (absorption_bars * 1.2) +
        (fakeout_count * 1.5)
    )
    score = int(np.clip(round(raw_score), 1, 10))

    if score >= 7:
        text = "высокий уровень манипуляций сохраняется. Крупные игроки продолжают активно работать в этом диапазоне."
    elif score >= 5:
        text = "умеренная манипулятивная активность на локальных уровнях."
    else:
        text = "низкий уровень манипуляций. Рынок движется в естественном потоке ордеров."

    return score, text


def calculate_zone_strength(df: pd.DataFrame, window: int = 50) -> tuple[int, str]:
    """
    Computes Zone Strength (0% - 100%):
    - Evaluates the durability of the current key support/resistance or order block level.
    - Repeated tests without bounce weaken the zone (liquidity exhaustion).
    - Strong momentum into the zone reduces zone holding probability.
    """
    if len(df) < 10:
        return 50, "зона со средней силой удержания."

    slice_df = df.tail(window).copy()
    current_close = slice_df["close"].iloc[-1]
    
    # Identify nearest swing level
    swing_highs = slice_df["high"].rolling(10, min_periods=1).max()
    swing_lows = slice_df["low"].rolling(10, min_periods=1).min()
    
    # Proximity to support or resistance
    dist_high = abs(current_close - swing_highs.iloc[-1])
    dist_low = abs(current_close - swing_lows.iloc[-1])
    
    # Touch frequency: how many times price hovered in 0.2% vicinity of level
    level_price = swing_highs.iloc[-1] if dist_high < dist_low else swing_lows.iloc[-1]
    threshold = level_price * 0.002
    touches = (abs(slice_df["close"] - level_price) < threshold).sum()

    # Volume at level
    vol = slice_df["volume"] if "volume" in slice_df.columns else pd.Series(1.0, index=slice_df.index)
    level_vol = vol[abs(slice_df["close"] - level_price) < threshold].sum()
    total_vol = vol.sum() if vol.sum() > 0 else 1.0
    vol_share = level_vol / total_vol

    # Exhaustion rule: each subsequent retest depletes limit orders
    if touches >= 5:
        base_strength = 20.0
    elif touches >= 3:
        base_strength = 45.0
    else:
        base_strength = 75.0

    strength = int(np.clip(round(base_strength + (vol_share * 20.0) - (touches * 3.0)), 5, 95))

    if strength <= 30:
        text = "зона крайне слабая. Текущий уровень не является серьёзной поддержкой, вероятность ухода ниже высокая."
    elif strength <= 60:
        text = "зона умеренной силы. Возможна локальная консолидация перед импульсом."
    else:
        text = "сильная институциональная зона ликвидности с высоким потенциалом отскока."

    return strength, text


def calculate_smf_ratio(df: pd.DataFrame, window: int = 30) -> tuple[float, str]:
    """
    Computes Smart Money Flow Ratio (SMF Ratio):
    Ratio of institutional order flow (large bars with clean directional progression)
    vs retail flow (small high-churn candles).
    """
    if len(df) < 5:
        return 1.0, "баланс институционального и розничного потока."

    slice_df = df.tail(window).copy()
    vol = slice_df["volume"] if "volume" in slice_df.columns else pd.Series(1.0, index=slice_df.index)
    vol_median = vol.median() if vol.median() > 0 else 1.0

    large_mask = vol > vol_median
    small_mask = ~large_mask

    # Price progress per volume unit
    large_progression = (slice_df.loc[large_mask, "close"].diff().abs() * vol.loc[large_mask]).sum()
    small_progression = (slice_df.loc[small_mask, "close"].diff().abs() * vol.loc[small_mask]).sum()

    if small_progression <= 0:
        ratio = 2.0
    else:
        ratio = large_progression / max(small_progression, 1e-6)

    ratio = round(float(np.clip(ratio, 0.5, 5.0)), 2)

    # Determine directional bias of smart money
    recent_delta = slice_df["close"].iloc[-1] - slice_df["close"].iloc[-5]
    dir_word = "вниз" if recent_delta <= 0 else "вверх"

    if ratio >= 2.0:
        text = f"институционалы доминируют над розницей с коэффициентом {ratio:.1f} к 1. Умные деньги продолжают давить {dir_word}."
    elif ratio >= 1.2:
        text = f"преобладание институционального потока ({ratio:.2f}x). Направленное движение {dir_word}."
    else:
        text = "паритет институциональных и розничных участников."

    return ratio, text


def calculate_liquidity_grab(df: pd.DataFrame, window: int = 30) -> tuple[int, str]:
    """
    Computes Liquidity Grab Score (1-10):
    Detects stop-hunting sweeps beyond prior key highs/lows followed by immediate rejection.
    """
    if len(df) < 10:
        return 5, "умеренная охота за ликвидностью."

    slice_df = df.tail(window).copy()
    
    # 20-bar swing extremes
    sw_high = slice_df["high"].rolling(15, min_periods=1).max().shift(1)
    sw_low = slice_df["low"].rolling(15, min_periods=1).min().shift(1)

    # Sweeps: High pierced, but Close returned below; Low pierced, but Close returned above
    high_sweeps = (slice_df["high"] > sw_high) & (slice_df["close"] < sw_high)
    low_sweeps = (slice_df["low"] < sw_low) & (slice_df["close"] > sw_low)

    # Sweep magnitude
    high_wick = slice_df["high"] - slice_df[["open", "close"]].max(axis=1)
    low_wick = slice_df[["open", "close"]].min(axis=1) - slice_df["low"]

    sweep_count = (high_sweeps | low_sweeps).sum()
    avg_sweep_wick = float((high_wick[high_sweeps].sum() + low_wick[low_sweeps].sum()) / max(sweep_count, 1))

    atr = (slice_df["high"] - slice_df["low"]).mean()
    wick_atr_ratio = avg_sweep_wick / max(atr, 1e-6)

    score = int(np.clip(round(sweep_count * 2.0 + wick_atr_ratio * 3.0 + 2), 1, 10))

    if score >= 7:
        text = "активная охота за ликвидностью. Именно это объясняет резкие движения на локальных уровнях перед продолжением тренда."
    elif score >= 4:
        text = "локальные сборы стопов вблизи ключевых экстремумов."
    else:
        text = "спокойный рынок, сбор стопов не выражен."

    return score, text


def calculate_delta_confidence(df: pd.DataFrame) -> tuple[str, str]:
    """
    Computes Delta Confidence (LOW / MEDIUM / HIGH / VERY HIGH):
    Multi-timeframe consistency of order flow volume delta and directional pressure.
    """
    if len(df) < 10:
        return "MEDIUM", "умеренная согласованность дельты."

    slice_df = df.tail(30).copy()
    hl = (slice_df["high"] - slice_df["low"]).replace(0, np.nan)
    pos_in_bar = (slice_df["close"] - slice_df["low"]) / hl
    vol = slice_df["volume"] if "volume" in slice_df.columns else pd.Series(1.0, index=slice_df.index)

    signed_delta = (pos_in_bar * 2.0 - 1.0).fillna(0.0) * vol
    cum_delta = signed_delta.cumsum()
    
    # Delta slope
    slope = cum_delta.iloc[-1] - cum_delta.iloc[0]
    total_vol = vol.sum() if vol.sum() > 0 else 1.0
    norm_slope = abs(slope) / total_vol

    # Consistency across bars
    direction_bars = (signed_delta > 0).sum() if slope > 0 else (signed_delta < 0).sum()
    consistency = direction_bars / len(slice_df)

    dir_party = "Покупатели" if slope > 0 else "Продавцы"
    
    if norm_slope > 0.4 and consistency > 0.65:
        level = "HIGH"
        text = f"уверенность модели в направлении дельты высокая. {dir_party} контролируют рынок на старших таймфреймах."
    elif norm_slope > 0.6 and consistency > 0.75:
        level = "VERY HIGH"
        text = f"сверхвысокая уверенность в дельте. Полный институциональный контроль ({dir_party})."
    elif norm_slope > 0.2:
        level = "MEDIUM"
        text = f"умеренная уверенность в дельте. Преимущество на стороне {dir_party}."
    else:
        level = "LOW"
        text = "низкая уверенность в дельте. Рынок находится в балансе покупателей и продавцов."

    return level, text


def compute_institutional_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Aggregates all 5 institutional microstructure metrics into a structured dictionary.
    """
    manip_score, manip_text = calculate_manipulation_index(df)
    zone_score, zone_text = calculate_zone_strength(df)
    smf_ratio, smf_text = calculate_smf_ratio(df)
    liq_score, liq_text = calculate_liquidity_grab(df)
    delta_conf, delta_text = calculate_delta_confidence(df)

    return {
        "manipulation_index": {
            "score": manip_score,
            "max": 10,
            "text": manip_text,
            "display": f"{manip_score}/10",
        },
        "zone_strength": {
            "score": zone_score,
            "max": 100,
            "text": zone_text,
            "display": f"{zone_score}%",
        },
        "smf_ratio": {
            "ratio": smf_ratio,
            "text": smf_text,
            "display": f"{smf_ratio:.2f}",
        },
        "liquidity_grab": {
            "score": liq_score,
            "max": 10,
            "text": liq_text,
            "display": f"{liq_score}/10",
        },
        "delta_confidence": {
            "level": delta_conf,
            "text": delta_text,
            "display": delta_conf,
        },
    }


def format_institutional_metrics_report(metrics: Dict[str, Any]) -> str:
    """
    Formats the exact textual report matching the institutional analytics format:
    *Метрики по софту на текущий момент*
    ...
    """
    m = metrics
    return (
        "📊 *Метрики по софту на текущий момент*\n\n"
        f"**Manipulation Index: {m['manipulation_index']['display']}** — {m['manipulation_index']['text']}\n\n"
        f"**Zone Strength: {m['zone_strength']['display']}** — {m['zone_strength']['text']}\n\n"
        f"**SMF Ratio: {m['smf_ratio']['display']}** — {m['smf_ratio']['text']}\n\n"
        f"**Liquidity Grab: {m['liquidity_grab']['display']}** — {m['liquidity_grab']['text']}\n\n"
        f"**Delta Confidence: {m['delta_confidence']['display']}** — {m['delta_confidence']['text']}"
    )
