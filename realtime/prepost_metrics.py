"""Pre/post-fix walk-forward comparison metrics for the dashboard.

Reads the per-trade CSVs written by scripts/diag_btc_eur_prepost.py
(logs/dir_prepost_<asset>[_<tf>]_prefix.csv / _postfix.csv). Each row is one
walk-forward trade with columns:
  fold_id, variant, entry_ts, direction, session, regime,
  p_long, p_short, p_max, pnl, R, exit_reason

Aggregates per asset: n, WR, PF, sum_R, mean_R, long/short split, sum_pnl —
separately for the PRE-fix leg (the historical +3h-shifted timestamps and old
session scheme) and the POST-fix leg (true UTC + corrected sessions). The
panel lets the operator see, in one table, whether the timestamp fix changed
the walk-forward outcome of each of the 5 assets.
"""
from __future__ import annotations

import glob
import os
import re
from datetime import UTC
from typing import Any, Optional

import numpy as np
import pandas as pd

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _block_bootstrap_sumR_ci(
    r_values: list[float],
    n_boot: int = 5000,
    block: int = 20,
    confidence: float = 0.95,
    seed: int = 42,
) -> tuple[float | None, float | None]:
    """Block-bootstrap 95% CI for sum(R).

    Resamples the R-series with overlapping blocks to preserve serial
    dependence (same-day/regime clustering). Returns (ci_low, ci_high)
    or (None, None) when the input is too short or degenerate.
    """
    arr = np.asarray(r_values, dtype=float)
    n = len(arr)
    if n < 3:
        return (None, None)
    block = max(1, min(block, n - 1))
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    sums = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n - block, size=nb)
        sample = np.concatenate([arr[s:s + block] for s in starts])[:n]
        sums[i] = sample.sum()
    alpha = (1.0 - confidence) / 2.0
    ci_low = float(np.percentile(sums, alpha * 100))
    ci_high = float(np.percentile(sums, (1.0 - alpha) * 100))
    return (round(ci_low, 3), round(ci_high, 3))


def _metrics_for(df_slice: pd.DataFrame, compute_ci: bool = False) -> dict[str, Any]:
    n = len(df_slice)
    if n == 0:
        return {"n": 0, "wr_pct": None, "pf": None, "sum_r": 0.0, "mean_r": None,
                "sum_pnl": 0.0, "long_n": 0, "short_n": 0,
                "sum_r_ci": [None, None]}
    r = df_slice["R"].astype(float)
    wins = r[r > 0]
    losses = r[r <= 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    pf = (gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0)
    long_n = int((df_slice["direction"].astype(str).str.lower().isin(["long", "buy", "l"])).sum())
    short_n = n - long_n
    ci = _block_bootstrap_sumR_ci(r.tolist()) if compute_ci else [None, None]
    return {
        "n": n,
        "wr_pct": round(100.0 * len(wins) / n, 1),
        "pf": round(pf, 2),
        "sum_r": round(float(r.sum()), 3),
        "mean_r": round(float(r.mean()), 4),
        "sum_pnl": round(float(pd.to_numeric(df_slice["pnl"], errors="coerce").sum()), 2),
        "long_n": long_n,
        "short_n": short_n,
        "sum_r_ci": ci,
    }


def _load_trades(csv_path: str) -> pd.DataFrame:
    """Load per-trade CSV and normalise direction column."""
    df = pd.read_csv(csv_path)
    if "direction" in df.columns:
        d = df["direction"].astype(str).str.lower()
        df["_dir_norm"] = d.map({"long": "long", "buy": "long", "l": "long",
                                   "short": "short", "sell": "short", "s": "short"}).fillna(d)
    else:
        df["_dir_norm"] = "unknown"
    return df


def filtered_metrics(
    df: pd.DataFrame,
    session: str | None = None,
    direction: str | None = None,
    compute_ci: bool = False,
) -> dict[str, Any]:
    """Aggregate metrics with optional session/direction filters."""
    mask = pd.Series(True, index=df.index)
    if session and session != "all":
        mask &= df["session"].astype(str).str.lower() == session.lower()
    if direction and direction != "all":
        d = direction.lower()
        mask &= df["_dir_norm"].isin([d, d.rstrip("s")])  # long/short
    return _metrics_for(df[mask], compute_ci=compute_ci)


_ASSET_RE = re.compile(r"dir_prepost_([a-z]+?)(?:_(m\d+|h\d+|h4|d1))?_(prefix|postfix)\.csv$")


def collect_prepost(log_dir: Optional[str] = None) -> dict[str, Any]:
    """Scan the log dir for pre/post walk-forward CSV pairs.

    Returns a dict:
      {
        "available": bool,
        "assets": {ASSET: {"tf": "...", "pre": {...}, "post": {...}, "delta": {...}}},
        "as_of_utc": ISO timestamp of the newest CSV mtime,
      }
    When several files exist for one asset (e.g. btcusd and btcusd_m5), the
    most recent pair wins; the others are listed under "extra_pairs".
    """
    log_dir = log_dir or LOG_DIR
    files = sorted(glob.glob(os.path.join(log_dir, "dir_prepost_*.csv")))
    by_pair: dict[tuple[str, str], dict[str, str]] = {}  # (asset, tf) -> {pre|post: path}
    for fp in files:
        m = _ASSET_RE.match(os.path.basename(fp))
        if not m:
            continue
        asset_lower, tf, leg = m.group(1), m.group(2) or "", m.group(3)
        asset = asset_lower.upper()
        key = "pre" if leg == "prefix" else "post"
        by_pair.setdefault((asset, tf), {})[key] = fp

    assets: dict[str, Any] = {}
    extra: list[dict[str, Any]] = []
    for (asset, tf), pair in by_pair.items():
        if "pre" not in pair or "post" not in pair:
            extra.append({"asset": asset, "tf": tf, "missing": sorted(pair)})
            continue
        rec = {"tf": tf or None, "pre": None, "post": None, "delta": None}
        try:
            pre_df = pd.read_csv(pair["pre"])
            post_df = pd.read_csv(pair["post"])
        except Exception as exc:  # corrupt file -> keep the row but mark it
            extra.append({"asset": asset, "tf": tf, "error": str(exc)})
            continue
        rec["pre"] = _metrics_for(pre_df)
        rec["post"] = _metrics_for(post_df)
        # Store raw trade counts for filter dropdowns
        rec["pre_sessions"] = sorted(pre_df["session"].dropna().unique().tolist()) if "session" in pre_df else []
        rec["post_sessions"] = sorted(post_df["session"].dropna().unique().tolist()) if "session" in post_df else []
        rec["delta"] = {
            "sum_r": round((rec["post"]["sum_r"] or 0) - (rec["pre"]["sum_r"] or 0), 3),
            "sum_pnl": round((rec["post"]["sum_pnl"] or 0) - (rec["pre"]["sum_pnl"] or 0), 2),
            "wr_pct": (None if rec["post"]["wr_pct"] is None or rec["pre"]["wr_pct"] is None
                       else round(rec["post"]["wr_pct"] - rec["pre"]["wr_pct"], 1)),
            "pf": (None if rec["post"]["pf"] is None or rec["pre"]["pf"] is None
                   else round(rec["post"]["pf"] - rec["pre"]["pf"], 2)),
            "n": (rec["post"]["n"] or 0) - (rec["pre"]["n"] or 0),
        }
        # Keep the newest file's pair when the same asset has several TFs.
        existing = assets.get(asset)
        if existing is None or os.path.getmtime(pair["post"]) > os.path.getmtime(existing["_post_path"]):
            if existing is not None:
                extra.append({"asset": asset, "tf": existing["tf"], "superseded_by": tf or "default"})
            assets[asset] = {**rec, "_post_path": pair["post"]}
        else:
            extra.append({"asset": asset, "tf": tf, "superseded_by": existing["tf"] or "default"})

    mtimes = [os.path.getmtime(fp) for fp in files if os.path.exists(fp)]
    as_of = None
    if mtimes:
        from datetime import datetime
        as_of = datetime.fromtimestamp(max(mtimes), tz=UTC).isoformat()

    # Strip the internal bookkeeping key before handing the payload to the API.
    for a in assets.values():
        a.pop("_post_path", None)

    return {
        "available": bool(assets),
        "assets": assets,
        "extra_pairs": extra,
        "as_of_utc": as_of,
    }


def collect_prepost_filtered(
    asset_key: str,
    session: str | None = None,
    direction: str | None = None,
    log_dir: str | None = None,
) -> dict[str, Any]:
    """Return filtered pre/post metrics for one asset.

    Used by the /api/prepost endpoint to support session/direction dropdowns.
    Returns the same structure as the top-level asset entry in collect_prepost,
    plus the available filter values for the UI.
    """
    log_dir = log_dir or LOG_DIR
    # Find the newest pre/post pair for this asset
    pattern = os.path.join(log_dir, f"dir_prepost_{asset_key.lower()}*.csv")
    files = sorted(glob.glob(pattern))
    by_pair: dict[str, str] = {}
    tf = None
    for fp in files:
        m = _ASSET_RE.match(os.path.basename(fp))
        if not m:
            continue
        asset, file_tf, leg = m.group(1).upper(), m.group(2), m.group(3)
        if asset != asset_key.upper():
            continue
        key = "pre" if leg == "prefix" else "post"
        by_pair[key] = fp
        if file_tf:
            tf = file_tf

    if "pre" not in by_pair or "post" not in by_pair:
        return {"available": False, "reason": "missing_files"}

    pre_df = _load_trades(by_pair["pre"])
    post_df = _load_trades(by_pair["post"])

    # Collect available filter values
    all_sessions = sorted(set(pre_df["session"].dropna().tolist() + post_df["session"].dropna().tolist()))
    all_directions = sorted(set(pre_df["_dir_norm"].dropna().tolist() + post_df["_dir_norm"].dropna().tolist()))

    pre_m = filtered_metrics(pre_df, session, direction, compute_ci=True)
    post_m = filtered_metrics(post_df, session, direction, compute_ci=True)
    delta = {
        "sum_r": round((post_m["sum_r"] or 0) - (pre_m["sum_r"] or 0), 3),
        "sum_pnl": round((post_m["sum_pnl"] or 0) - (pre_m["sum_pnl"] or 0), 2),
        "wr_pct": (None if post_m["wr_pct"] is None or pre_m["wr_pct"] is None
                   else round(post_m["wr_pct"] - pre_m["wr_pct"], 1)),
        "pf": (None if post_m["pf"] is None or pre_m["pf"] is None
               else round(post_m["pf"] - pre_m["pf"], 2)),
        "n": (post_m["n"] or 0) - (pre_m["n"] or 0),
    }

    return {
        "available": True,
        "tf": tf,
        "pre": pre_m,
        "post": post_m,
        "delta": delta,
        "sessions": all_sessions,
        "directions": all_directions,
    }
