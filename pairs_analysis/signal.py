# -*- coding: utf-8 -*-
"""SignalEngine (ТЗ §4.4, §6): mean-reversion signals + exit rules.

Signals (все условия AND, пороги из конфига):
  - MEAN-REV SHORT (спред): z > +entry_z AND ADF p < adf_p_max AND half-life
    в half_life_range_days AND Hurst < hurst_meanrev_max AND |z| <= stop_z.
  - MEAN-REV LONG  (спред): z < -entry_z, те же гейты.
  - NO EDGE — STAND ASIDE: |z| < entry_z (или не прошёл гейты).

Выход из парной позиции (ТЗ §4.4):
  - z пересёк exit_z (0)  -> "exit_z" (фиксация прибыли);
  - |z| > stop_z         -> "stop_z" (стоп по спреду);
  - прошло > 2xHL баров  -> "timeout";
  - конец данных         -> "end_of_data".

Backtest (ТЗ §7.2) — point-in-time walk-forward: β_t из прямого фильтра
Калмана (только данные <= t), z по скользящему окну, гейты (ADF/HL/Hurst)
считаются по окну ДО текущего бара. Никакого look-ahead.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import metrics as metrics_mod
from .analyzer import BARS_PER_DAY, PairMetrics


@dataclass
class PairTrade:
    """Одна закрытая парная сделка."""
    entry_ts: str
    entry_z: float
    exit_ts: str
    exit_z: float
    exit_reason: str            # exit_z | stop_z | timeout | end_of_data
    r: float
    bars_held: int
    half_life_bars: float
    adf_p: float
    hurst: float
    beta: float
    side: str                   # long P1 / short P2, либо наоборот

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class BacktestResult:
    name: str
    timeframe: str
    n_bars: int
    trades: list = field(default_factory=list)

    def summary(self) -> dict:
        rs = [t.r for t in self.trades]
        n = len(rs)
        if n == 0:
            return {"name": self.name, "timeframe": self.timeframe,
                    "n_bars": self.n_bars, "n_trades": 0}
        wins = sum(1 for r in rs if r > 0)
        # макс-просадка серии R
        cum = np.cumsum(rs)
        peak = np.maximum.accumulate(cum)
        mdd = float(np.min(cum - peak))
        held = [t.bars_held for t in self.trades]
        held_sorted = sorted(held)
        by_z: dict[str, dict] = {}
        for lo, hi in ((2.0, 2.5), (2.5, 3.0)):
            sel = [t.r for t in self.trades if lo <= abs(t.entry_z) < hi]
            if sel:
                by_z[f"{lo:.1f}-{hi:.1f}σ"] = {"n": len(sel), "avg_r": round(sum(sel) / len(sel), 3)}
        by_reason: dict[str, int] = {}
        for t in self.trades:
            by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
        return {
            "name": self.name, "timeframe": self.timeframe,
            "n_bars": self.n_bars, "n_trades": n,
            "win_rate": round(100 * wins / n, 1),
            "sum_r": round(sum(rs), 2), "avg_r": round(sum(rs) / n, 3),
            "max_dd_r": round(mdd, 2),
            "avg_bars_held": round(sum(held) / n, 1),
            "median_bars_held": float(np.median(held)),
            "by_entry_z": by_z, "by_reason": by_reason,
        }


@dataclass
class Signal:
    """Текущий сигнал по паре (живой снимок, ТЗ §6 интерфейс)."""
    name: str
    timeframe: str
    ts: str
    z: float
    direction: str              # long | short | none
    reason: str
    adf_p: float
    half_life_days: float
    hurst: float
    valid: bool


def _simulate_position(side: str, entry_idx: int, entry_z: float,
                       z: np.ndarray, stop_z: float, exit_z: float,
                       hl_bars: float, last_idx: int):
    """Проводит открытую позицию по ряду z до первого исхода.
    Возвращает (exit_idx, reason, z_at_exit). Чистая функция — тестируемая."""
    timeout = int(math.ceil(2.0 * hl_bars)) if np.isfinite(hl_bars) else 10**9
    for t in range(entry_idx + 1, last_idx + 1):
        zt = z[t]
        if np.isnan(zt):
            continue
        if side == "long" and zt >= exit_z:
            return t, "exit_z", zt
        if side == "short" and zt <= exit_z:
            return t, "exit_z", zt
        if abs(zt) >= stop_z:
            return t, "stop_z", zt
        if t - entry_idx >= timeout:
            return t, "timeout", zt
    return last_idx, "end_of_data", z[last_idx]


class SignalEngine:
    """Пороги (ТЗ §4.4): entry_z, exit_z, stop_z, adf_p_max,
    half_life_range_days, hurst_meanrev_max. cfg: окна анализа."""

    def __init__(self, thresholds: dict | None = None, cfg: dict | None = None):
        self.t = thresholds or {}
        self.cfg = cfg or {}
        self.entry_z = float(self.t.get("entry_z", 2.0))
        self.exit_z = float(self.t.get("exit_z", 0.0))
        self.stop_z = float(self.t.get("stop_z", 3.0))
        self.adf_p_max = float(self.cfg.get("adf_p_max", 0.05))
        hl = self.cfg.get("half_life_range_days", [2.0, 60.0])
        self.hl_min_days, self.hl_max_days = float(hl[0]), float(hl[1])
        self.hurst_max = float(self.cfg.get("hurst_meanrev_max", 0.5))
        self.window = int(self.cfg.get("window", 90))
        self.gate_window = int(self.cfg.get("gate_window", 250))
        self.kalman_q = float(self.cfg.get("kalman_q", 1e-4))
        self.kalman_r = float(self.cfg.get("kalman_r", 1e-2))

    # ---- гейты (point-in-time: только данные <= t) ----
    def _gates(self, e: pd.Series, t: int) -> dict:
        lo = max(0, t + 1 - self.gate_window)
        tail = e.iloc[lo:t + 1]
        adf_p = metrics_mod.adf_pvalue(tail)
        theta, hl_bars = metrics_mod.half_life(tail)
        hurst = metrics_mod.hurst_rs(tail.diff().dropna())
        return {"adf_p": adf_p, "theta": theta, "half_life_bars": hl_bars,
                "hurst": hurst}

    def _gates_ok(self, g: dict, tf: str) -> tuple[bool, str]:
        bars_per_day = BARS_PER_DAY.get(tf, 1.0)
        hl_days = g["half_life_bars"] / bars_per_day
        if not np.isfinite(g["adf_p"]) or g["adf_p"] >= self.adf_p_max:
            return False, f"ADF p={g['adf_p']:.3f} ≥ {self.adf_p_max}"
        if not np.isfinite(hl_days) or not (self.hl_min_days <= hl_days <= self.hl_max_days):
            return False, f"HL {hl_days:.1f}д вне [{self.hl_min_days:.0f},{self.hl_max_days:.0f}]"
        if not np.isfinite(g["hurst"]) or g["hurst"] >= self.hurst_max:
            return False, f"Hurst {g['hurst']:.2f} ≥ {self.hurst_max} (трендовый режим)"
        return True, "ok"

    # ---- walk-forward бэктест (ТЗ §7.2) ----
    def walk_forward(self, p1: pd.DataFrame, p2: pd.DataFrame,
                     name: str, timeframe: str) -> BacktestResult:
        ln1 = np.log(p1["close"].astype(float))
        ln2 = np.log(p2["close"].astype(float))
        if len(ln1) < self.window + 50:
            return BacktestResult(name, timeframe, len(ln1))
        beta_series = pd.Series(
            metrics_mod.kalman_beta(ln2, ln1, q=self.kalman_q, r=self.kalman_r),
            index=ln1.index)
        e = ln1 - beta_series * ln2
        z = metrics_mod.zscore(e, self.window).to_numpy(dtype=float)
        min_start = max(self.window, int(self.cfg.get("min_start_bars", 250)))

        trades: list[PairTrade] = []
        cooldown = False        # после выхода ждём |z| < entry_z (анти-чатер)
        n = len(ln1)
        t = min_start
        while t < n:
            zt = z[t]
            if np.isnan(zt):
                t += 1
                continue
            if cooldown and abs(zt) < self.entry_z:
                cooldown = False
            # вход: |z| в [entry_z, stop_z) (строго ниже стопа — иначе риск=0)
            if not cooldown and self.entry_z <= abs(zt) < self.stop_z:
                g = self._gates(e, t)
                ok, _why = self._gates_ok(g, timeframe)
                if ok:
                    side = "short" if zt > 0 else "long"
                    # позиция симулируется до конца и время перепрыгивает на
                    # выход: сделки не перекрываются, look-ahead нет
                    exit_idx, reason, z_exit = _simulate_position(
                        side, t, zt, z, self.stop_z, self.exit_z,
                        g["half_life_bars"], n - 1)
                    if side == "long":
                        r = (z_exit - zt) / (self.stop_z + zt)          # risk = stop_z - |z0|
                    else:
                        r = (zt - z_exit) / (self.stop_z - zt)
                    trades.append(PairTrade(
                        entry_ts=str(ln1.index[t].date()), entry_z=round(float(zt), 3),
                        exit_ts=str(ln1.index[exit_idx].date()),
                        exit_z=round(float(z_exit), 3), exit_reason=reason,
                        r=round(float(r), 3), bars_held=exit_idx - t,
                        half_life_bars=round(float(g["half_life_bars"]), 1),
                        adf_p=round(float(g["adf_p"]), 5),
                        hurst=round(float(g["hurst"]), 3),
                        beta=round(float(beta_series.iloc[t]), 4),
                        side=side))
                    t = exit_idx
                    cooldown = True
                    continue
            t += 1
        return BacktestResult(name, timeframe, n, trades)

    # ---- живой снимок по готовым метрикам (ТЗ §6 интерфейс) ----
    def current(self, m: PairMetrics) -> Signal:
        zs = m.zscore.dropna()
        if not len(zs):
            return Signal(m.name, m.timeframe, m.end, float("nan"), "none",
                          "нет данных", float("nan"), float("nan"), float("nan"), False)
        z_cur = float(zs.iloc[-1])
        g = {"adf_p": m.adf_p, "half_life_bars": m.half_life_bars, "hurst": m.hurst}
        ok, why = self._gates_ok(g, m.timeframe)
        if not ok:
            return Signal(m.name, m.timeframe, m.end, z_cur, "none",
                          f"NO EDGE — гейты не пройдены ({why})",
                          m.adf_p, m.half_life_days, m.hurst, False)
        if z_cur >= self.entry_z:
            return Signal(m.name, m.timeframe, m.end, z_cur, "short",
                          "MEAN-REV SHORT (z > +2σ, ADF/HL/Hurst ок)",
                          m.adf_p, m.half_life_days, m.hurst, True)
        if z_cur <= -self.entry_z:
            return Signal(m.name, m.timeframe, m.end, z_cur, "long",
                          "MEAN-REV LONG (z < −2σ, ADF/HL/Hurst ок)",
                          m.adf_p, m.half_life_days, m.hurst, True)
        return Signal(m.name, m.timeframe, m.end, z_cur, "none",
                      f"NO EDGE — STAND ASIDE (|z|={abs(z_cur):.2f} < {self.entry_z:.0f}σ)",
                      m.adf_p, m.half_life_days, m.hurst, False)
