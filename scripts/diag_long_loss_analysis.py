"""Decompose post-fix XAUUSD long walk-forward losses.

Reads logs/trade_quality_xauusd_dir_postfix.csv (294 trades, deterministic
walk-forward artifact) and slices the LONG side by session, regime, fold,
UTC hour, entry-probability bucket and exit reason, plus cross-tabs, to
locate where the -9.92R total is lost.

Read-only: does not re-run the walk-forward, does not touch the DB.
"""
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

CSV = os.path.join("logs", "trade_quality_xauusd_dir_postfix.csv")


def metrics(df: pd.DataFrame) -> dict:
    if len(df) == 0:
        return {"n": 0, "WR%": float("nan"), "PF": float("nan"),
                "sum_R": 0.0, "avg_win_R": float("nan"), "avg_loss_R": float("nan"),
                "R_mean": float("nan")}
    r = df["R"].astype(float)
    wins = r[r > 0]
    losses = r[r <= 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    # Breakeven WR given average win/loss sizes (counts may differ):
    # WR*avg_win = (1-WR)*|avg_loss|  ->  WR = |avg_loss| / (avg_win + |avg_loss|)
    aw = float(wins.mean()) if len(wins) else float("nan")
    al = float(-losses.mean()) if len(losses) else float("nan")
    be_wr = al / (aw + al) * 100 if (aw + al) > 0 else float("nan")
    return {
        "n": len(df),
        "WR%": round(100.0 * len(wins) / len(df), 1),
        "PF": round(pf, 2) if pf != 999.0 else 999.0,
        "sum_R": round(float(r.sum()), 3),
        "avg_win_R": round(aw, 3) if not np.isnan(aw) else float("nan"),
        "avg_loss_R": round(al, 3) if not np.isnan(al) else float("nan"),
        "BE_WR%": round(be_wr, 1) if not np.isnan(be_wr) else float("nan"),
        "R_mean": round(float(r.mean()), 4),
    }


def show(title: str, rows: pd.DataFrame, value_cols: list[str] | None = None) -> None:
    print(f"\n=== {title} ===")
    if rows is None or len(rows) == 0:
        print("  (empty)")
        return
    cols = [c for c in rows.columns if c != "n"]
    print(rows.to_string(index=False, float_format=lambda x: f"{x:.3f}"))


def main() -> None:
    df = pd.read_csv(CSV)
    longs = df[df["direction"] == "long"].copy()
    print(f"Total trades: {len(df)} | longs: {len(longs)}")

    # entry UTC hour + weekday from entry_ts
    longs["dt"] = pd.to_datetime(longs["entry_ts"], unit="s", utc=True)
    longs["hour_utc"] = longs["dt"].dt.hour
    longs["dow"] = longs["dt"].dt.dayofweek  # 0=Mon

    print("\n========== OVERALL ==========")
    show("ALL LONGS", pd.DataFrame([metrics(longs)]))
    show("ALL LONGS by exit_reason",
         longs.groupby("exit_reason").apply(lambda g: pd.Series(metrics(g))).reset_index())

    # ---- by session ----
    show("By SESSION (n, sum_R, R_mean)",
         longs.groupby("session").apply(lambda g: pd.Series(metrics(g))).reset_index().sort_values("sum_R"))

    # ---- by regime ----
    show("By REGIME", longs.groupby("regime").apply(lambda g: pd.Series(metrics(g))).reset_index().sort_values("sum_R"))

    # ---- by fold ----
    fold_m = longs.groupby("fold_id").apply(lambda g: pd.Series(metrics(g))).reset_index().sort_values("sum_R")
    show("By FOLD", fold_m)

    # ---- by UTC hour ----
    show("By UTC HOUR", longs.groupby("hour_utc").apply(lambda g: pd.Series(metrics(g))).reset_index().sort_values("sum_R"))

    # ---- by probability bucket ----
    bins = [0.0, 0.55, 0.60, 0.65, 0.70, 1.0]
    labels = ["<0.55", "0.55-0.60", "0.60-0.65", "0.65-0.70", ">=0.70"]
    longs["p_bucket"] = pd.cut(longs["p_long"], bins=bins, labels=labels, right=False)
    show("By P_LONG bucket", longs.groupby("p_bucket", observed=True).apply(lambda g: pd.Series(metrics(g))).reset_index().sort_values("sum_R"))

    # ---- by weekday ----
    show("By WEEKDAY (0=Mon)", longs.groupby("dow").apply(lambda g: pd.Series(metrics(g))).reset_index().sort_values("sum_R"))

    # ---- cross-tabs ----
    ct = longs.groupby(["session", "regime"], observed=True).apply(
        lambda g: pd.Series({"n": len(g), "sum_R": round(float(g["R"].sum()), 3),
                             "WR%": round(100.0 * (g["R"] > 0).mean(), 1)}),
        include_groups=False).reset_index().sort_values("sum_R")
    show("CROSS-TAB session x regime", ct)

    ct2 = longs.groupby(["hour_utc", "regime"], observed=True).apply(
        lambda g: pd.Series({"n": len(g), "sum_R": round(float(g["R"].sum()), 3),
                             "WR%": round(100.0 * (g["R"] > 0).mean(), 1)}),
        include_groups=False).reset_index().sort_values("sum_R")
    show("CROSS-TAB hour x regime", ct2)

    # ---- biggest single losers ----
    show("WORST 12 TRADES", longs.nsmallest(12, "R")[["entry_ts", "dt", "session", "regime", "p_long", "R", "exit_reason"]])

    # ---- losing streaks / clustering of losses by hour ----
    longs_sorted = longs.sort_values("entry_ts")
    longs_sorted["is_loss"] = longs_sorted["R"] <= 0
    # consecutive same-hour loss density
    hour_loss = longs_sorted.groupby("hour_utc")["is_loss"].agg(["count", "sum"])
    hour_loss["loss_pct"] = 100.0 * hour_loss["sum"] / hour_loss["count"]
    print("\n=== LOSS RATE BY HOUR ===")
    print(hour_loss.to_string(float_format=lambda x: f"{x:.1f}"))

    # breakeven math
    m = metrics(longs)
    print("\n========== BREAKEVEN MATH ==========")
    print(f"WR {m['WR%']}% vs breakeven WR {m['BE_WR%']}% -> edge deficit "
          f"{m['BE_WR%'] - m['WR%']:.1f}pp")
    print(f"avg win {m['avg_win_R']}R vs avg loss {m['avg_loss_R']}R "
          f"(ratio {abs(m['avg_loss_R'] / m['avg_win_R']):.2f}x)")


if __name__ == "__main__":
    main()
