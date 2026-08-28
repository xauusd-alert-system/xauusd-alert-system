"""
Plateau-based hyperparameter selection (quant audit Section 5 / Task 5, Grok/deepseek).

Argmax on noisy out-of-sample backtests selects sharp, overfit spikes: single
parameter combinations that happened to catch lucky trades but collapse when
market dynamics shift slightly (immediate neighbors are negative).

`select_plateau_config` searches for wide "plateaus" where all immediate
neighbors (+/-1 step along each grid dimension) are positive, finds the widest
connected plateau, and returns its central/median configuration.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd


def _build_coord_map(param_grid: Dict[str, List[Any]]) -> Tuple[List[str], Dict[Tuple, int], List[Dict[str, Any]]]:
    """Builds coordinate mappings for discrete parameter grids."""
    dims = list(param_grid.keys())
    sorted_values = {k: list(v) for k, v in param_grid.items()}
    val_to_idx = {k: {val: idx for idx, val in enumerate(vals)} for k, vals in sorted_values.items()}

    return dims, val_to_idx, sorted_values


def select_plateau_config(
    grid_results: pd.DataFrame | List[Dict[str, Any]],
    param_grid: Dict[str, List[Any]],
    metric_col: str = "oos_metric",
    min_metric: float = 0.0,
) -> Dict[str, Any]:
    """Selects the central configuration of the widest positive plateau.

    Parameters
    ----------
    grid_results : DataFrame or list of dicts containing parameter columns and `metric_col`.
    param_grid : Dict mapping parameter name to list of sorted discrete values.
    metric_col : Name of OOS performance metric (default 'oos_metric', falls back to 'pnl'/'sharpe').
    min_metric : Minimum acceptable metric value for positive edge (default 0.0).

    Returns
    -------
    Dict representing the chosen configuration parameters and plateau metadata.
    """
    df = pd.DataFrame(grid_results) if isinstance(grid_results, list) else grid_results.copy()
    if df.empty:
        return {}

    # Identify metric column
    if metric_col not in df.columns:
        for fallback in ["pnl", "total_pnl", "sharpe", "profit_factor", "expectancy"]:
            if fallback in df.columns:
                metric_col = fallback
                break

    dims, val_to_idx, sorted_values = _build_coord_map(param_grid)

    # Attach integer coordinate vector to each result row
    records = []
    coord_to_row = {}

    for row_idx, row in df.iterrows():
        coords = []
        valid = True
        for d in dims:
            val = row.get(d)
            if val in val_to_idx[d]:
                coords.append(val_to_idx[d][val])
            else:
                valid = False
                break
        if valid:
            c_tuple = tuple(coords)
            metric_val = float(row[metric_col]) if metric_col in row and pd.notna(row[metric_col]) else 0.0
            coord_to_row[c_tuple] = {
                "row_index": row_idx,
                "coords": c_tuple,
                "metric": metric_val,
                "params": {d: row[d] for d in dims},
                "row": row.to_dict(),
            }
            records.append(coord_to_row[c_tuple])

    if not records:
        # Fallback to argmax if coordinates cannot be matched
        best_row = df.loc[df[metric_col].idxmax()]
        return {d: best_row[d] for d in dims if d in best_row}

    # 1. Identify plateau points (point and all +/-1 neighbors in the grid have metric > min_metric)
    plateau_points = []
    d_dim = len(dims)

    for item in records:
        c = item["coords"]
        if item["metric"] <= min_metric:
            continue

        # Check all immediate neighbors (+/- 1 step along each axis)
        is_plateau_point = True
        neighbor_coords = []

        # Generate neighbor offsets in [-1, 0, 1]^d excluding (0, ..., 0)
        from itertools import product

        for offset in product([-1, 0, 1], repeat=d_dim):
            if all(o == 0 for o in offset):
                continue
            neighbor_c = tuple(c[k] + offset[k] for k in range(d_dim))
            # If neighbor is inside the grid boundary
            if all(0 <= neighbor_c[k] < len(sorted_values[dims[k]]) for k in range(d_dim)):
                if neighbor_c in coord_to_row:
                    neighbor_item = coord_to_row[neighbor_c]
                    if neighbor_item["metric"] <= min_metric:
                        is_plateau_point = False
                        break
                    neighbor_coords.append(neighbor_c)

        if is_plateau_point:
            plateau_points.append(item)

    # 2. Cluster plateau points into connected components
    if not plateau_points:
        # Fallback: find point with highest positive neighbor count or highest metric
        positive_records = [r for r in records if r["metric"] > min_metric]
        if positive_records:
            best_fallback = max(positive_records, key=lambda r: r["metric"])
            res = dict(best_fallback["params"])
            res["_selection_type"] = "fallback_positive"
            res["_metric"] = best_fallback["metric"]
            return res
        best_row = max(records, key=lambda r: r["metric"])
        res = dict(best_row["params"])
        res["_selection_type"] = "fallback_argmax"
        res["_metric"] = best_row["metric"]
        return res

    # Connected component analysis using BFS
    plateau_coord_set = {p["coords"]: p for p in plateau_points}
    visited = set()
    components = []

    for p_coord, p_item in plateau_coord_set.items():
        if p_coord in visited:
            continue
        cluster = []
        queue = [p_coord]
        visited.add(p_coord)

        while queue:
            curr = queue.pop(0)
            cluster.append(plateau_coord_set[curr])

            for offset in product([-1, 0, 1], repeat=d_dim):
                if all(o == 0 for o in offset):
                    continue
                nbr = tuple(curr[k] + offset[k] for k in range(d_dim))
                if nbr in plateau_coord_set and nbr not in visited:
                    visited.add(nbr)
                    queue.append(nbr)

        components.append(cluster)

    # 3. Select widest component (largest number of robust plateau configs)
    widest_component = max(components, key=len)

    # 4. Find the center / centroid of the widest plateau
    all_coords = np.array([p["coords"] for p in widest_component], dtype=float)
    centroid = np.mean(all_coords, axis=0)

    # Pick the member minimizing Euclidean distance to centroid (tie-break by metric)
    best_item = min(
        widest_component,
        key=lambda p: (
            np.sum((np.array(p["coords"]) - centroid) ** 2),
            -p["metric"],
        ),
    )

    result = dict(best_item["params"])
    result["_selection_type"] = "plateau_center"
    result["_plateau_size"] = len(widest_component)
    result["_metric"] = best_item["metric"]
    return result
