# -*- coding: utf-8 -*-
"""PairAnalyzer (ТЗ §6): one pair config -> PairMetrics.

Loads both legs (MT5 sqlite / CSV / Binance), resamples to the requested
timeframe, aligns on the common timestamp index, computes the log-spread
with a point-in-time Kalman β (OLS rolling as fallback), z-score, ADF,
OU half-life, σ and the price ratio — plus the math-board subset.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data as data_mod
from . import metrics as metrics_mod

BARS_PER_DAY = {"M5": 288.0, "M15": 96.0, "H1": 24.0, "H4": 6.0, "D1": 1.0}
BARS_PER_YEAR = {"M5": 72576.0, "M15": 24192.0, "H1": 6048.0, "H4": 1512.0, "D1": 252.0}


@dataclass
class PairMetrics:
    """Full result of one pair analysis (stage 1 core, ТЗ §4.1-§4.2)."""

    name: str
    timeframe: str
    n_bars: int
    start: str
    end: str
    beta: float
    beta_method: str
    beta_series: pd.Series
    spread: pd.Series
    zscore: pd.Series
    mu: float
    sigma: float
    sigma_annual: float
    adf_p: float
    theta: float
    half_life_bars: float
    half_life_days: float
    ratio: float
    p1_last: float
    p2_last: float
    formula_str: str
    # math board (ТЗ §4.2)
    hurst: float = float("nan")
    skew: float = float("nan")
    ex_kurtosis: float = float("nan")
    acf1: float = float("nan")
    realized_vol_pct: float = float("nan")
    # raw legs (for the dashboard)
    p1: pd.DataFrame = field(default=None, repr=False)
    p2: pd.DataFrame = field(default=None, repr=False)

    def summary(self) -> dict:
        return {
            "name": self.name,
            "timeframe": self.timeframe,
            "n_bars": self.n_bars,
            "start": self.start,
            "end": self.end,
            "beta": self.beta,
            "beta_method": self.beta_method,
            "z": float(self.zscore.dropna().iloc[-1]) if len(self.zscore.dropna()) else float("nan"),
            "mu": self.mu,
            "sigma": self.sigma,
            "sigma_annual": self.sigma_annual,
            "adf_p": self.adf_p,
            "theta": self.theta,
            "half_life_bars": self.half_life_bars,
            "half_life_days": self.half_life_days,
            "ratio": self.ratio,
            "hurst": self.hurst,
            "skew": self.skew,
            "ex_kurtosis": self.ex_kurtosis,
            "acf1": self.acf1,
            "realized_vol_pct": self.realized_vol_pct,
        }


class PairAnalyzer:
    """Computes PairMetrics for a single pair configuration.

    cfg: analysis section of config/pairs_config.yaml (window/ols/kalman/...).
    pair: one entry of the `pairs` list (name, source, symbols, ...).
    """

    def __init__(self, pair: dict, cfg: dict | None = None):
        self.pair = pair
        self.cfg = cfg or {}

    # ---- data loading ----
    def _load_leg(self, symbol: str, timeframe: str, start=None, end=None) -> pd.DataFrame:
        source = self.pair.get("source", "mt5")
        if source == "mt5":
            return data_mod.load_mt5(symbol, timeframe, db_path=self.cfg.get("mt5_db", data_mod.DEFAULT_DB))
        if source == "csv":
            paths = self.pair.get("paths") or {}
            path = paths.get(symbol) or paths.get(str(self.pair.get("symbols", [symbol])[0]))
            if not path:
                raise ValueError(f"пара {self.pair.get('name')}: нет CSV-пути для {symbol}")
            return data_mod.load_csv(path, timeframe)
        if source == "binance":
            return data_mod.fetch_binance(
                symbol, timeframe, start=start, end=end, cache_path=self.cfg.get("cache_db", data_mod.DEFAULT_CACHE)
            )
        raise ValueError(f"неизвестный source={source!r} (mt5 | csv | binance)")

    def analyze(self, timeframe: str | None = None) -> PairMetrics:
        pair = self.pair
        name = pair["name"]
        symbols = pair["symbols"]
        if len(symbols) != 2:
            raise ValueError(f"пара {name}: нужно ровно 2 инструмента")
        tf = (timeframe or self.cfg.get("default_timeframe", "D1")).upper()
        if tf not in BARS_PER_DAY:
            raise ValueError(f"неизвестный таймфрейм {tf!r}")

        p1 = self._load_leg(symbols[0], tf)
        p2 = self._load_leg(symbols[1], tf)
        p1, p2 = data_mod.align(p1, p2)
        if len(p1) < 20:
            raise ValueError(f"пара {name} на {tf}: слишком мало общих баров ({len(p1)})")

        window = int(self.cfg.get("window", 90))
        ols_win = int(self.cfg.get("ols_window", 90))
        kalman_q = float(self.cfg.get("kalman_q", 1e-4))
        kalman_r = float(self.cfg.get("kalman_r", 1e-2))

        ln1 = np.log(p1["close"].astype(float))
        ln2 = np.log(p2["close"].astype(float))

        # primary: Kalman (point-in-time); fallback: rolling OLS
        try:
            kb = metrics_mod.kalman_beta(ln2, ln1, q=kalman_q, r=kalman_r)
            beta_series = pd.Series(kb, index=ln1.index)
            beta_method = "kalman"
        except Exception:
            beta_series = metrics_mod.ols_beta(ln2, ln1, window=ols_win)
            beta_method = "ols"
        cur = beta_series.dropna()
        beta = float(cur.iloc[-1]) if len(cur) else float("nan")

        e = metrics_mod.spread(ln1, ln2, beta_series)
        z = metrics_mod.zscore(e, window)
        mu = float(e.rolling(window).mean().dropna().iloc[-1]) if len(e) >= window else float("nan")
        sigma = metrics_mod.spread_sigma(e, window)
        sigma_ann = metrics_mod.annualized_sigma(e, BARS_PER_YEAR.get(tf, 252.0), window)
        adf_p = metrics_mod.adf_pvalue(e)
        theta, hl_bars = metrics_mod.half_life(e)
        hl_days = hl_bars / BARS_PER_DAY.get(tf, 1.0) if np.isfinite(hl_bars) else float("inf")
        ratio = float(p1["close"].iloc[-1] / p2["close"].iloc[-1])

        formula = f"e_t = ln({ln1.iloc[-1]:.4f}) − {beta:.4f}·ln({ln2.iloc[-1]:.4f}) = {e.iloc[-1]:.4f}"

        # math board on the spread-return series (Hurst is R/S on the RETURNS —
        # on levels plain R/S mislabels mean-reverting series, see metrics.hurst_rs)
        r = e.diff().dropna()
        return PairMetrics(
            name=name,
            timeframe=tf,
            n_bars=len(p1),
            start=str(p1.index[0].date()),
            end=str(p1.index[-1].date()),
            beta=beta,
            beta_method=beta_method,
            beta_series=beta_series,
            spread=e,
            zscore=z,
            mu=mu,
            sigma=sigma,
            sigma_annual=sigma_ann,
            adf_p=adf_p,
            theta=theta,
            half_life_bars=hl_bars,
            half_life_days=hl_days,
            ratio=ratio,
            p1_last=float(p1["close"].iloc[-1]),
            p2_last=float(p2["close"].iloc[-1]),
            formula_str=formula,
            hurst=metrics_mod.hurst_rs(r),
            skew=metrics_mod.skew(r),
            ex_kurtosis=metrics_mod.excess_kurtosis(r),
            acf1=metrics_mod.acf1(e),
            realized_vol_pct=metrics_mod.realized_vol_pct(e, window),
            p1=p1,
            p2=p2,
        )


def analyze_all(cfg: dict, timeframe: str | None = None, only: str | None = None) -> list[PairMetrics]:
    """Run every pair in the config (or a named subset) and return metrics."""
    out = []
    for pair in cfg.get("pairs", []):
        if only and only.lower() not in pair["name"].lower():
            continue
        try:
            out.append(PairAnalyzer(pair, cfg.get("analysis", {})).analyze(timeframe))
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(f"  {pair['name']}: ПРОПУЩЕНА — {exc}")
    return out
