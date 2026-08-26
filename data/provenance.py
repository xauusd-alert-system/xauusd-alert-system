"""
Immutable data-provenance manifest for raw market-data exports (plan Wave 0).

Every backtest/fit that must be reproducible gets a manifest binding the raw
candle source to a fingerprint of its content:

* broker / terminal identity (company hash, build, data-path hash — supplied,
  never inferred from prices);
* canonical <-> broker symbol mapping and server timezone offset;
* source window, export time and per-year/per-session gap audit;
* sha256 ``data_hash`` over the canonical exported rows.

``verify_provenance_manifest`` recomputes the content fingerprint from the DB
and fails closed on any mismatch, so mixing brokers, truncated exports or
incomplete history stops the run instead of silently changing results.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from data.session_tagger import tag_session
from data.storage import read_candles

TIMEFRAME_INTERVAL_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400,
}

MANIFEST_SCHEMA_VERSION = 1


def _interval_seconds(timeframe: str) -> int:
    tf = timeframe.upper()
    if tf not in TIMEFRAME_INTERVAL_SECONDS:
        raise ValueError(f"unsupported timeframe for provenance: {timeframe!r}")
    return TIMEFRAME_INTERVAL_SECONDS[tf]


def _aligned_bars(start_ts: int, end_ts: int, interval: int) -> pd.DatetimeIndex:
    """Aligned bar timestamps in [start, end] on the epoch grid."""
    first = (start_ts // interval + (1 if start_ts % interval else 0)) * interval
    if first > end_ts:
        return pd.DatetimeIndex([])
    return pd.date_range(start=pd.Timestamp(first, unit="s", tz="UTC"),
                         end=pd.Timestamp(end_ts, unit="s", tz="UTC"),
                         freq=f"{interval}s", tz="UTC")


def _expected_bars_per_year(
    start_ts: int, end_ts: int, interval: int,
) -> dict[int, int]:
    """Expected aligned bars per UTC year, counting Mon-Fri only (weekend
    trading depends on the broker and is audited separately as weekend gaps)."""
    bars = _aligned_bars(start_ts, end_ts, interval)
    weekday_mask = bars.weekday < 5
    per_year: dict[int, int] = {}
    for ts in bars[weekday_mask]:
        year = ts.year
        per_year[year] = per_year.get(year, 0) + 1
    return per_year


def _session_of(ts: pd.Timestamp, sessions_config: dict) -> str:
    return tag_session(int(ts.timestamp()), sessions_config)


def gap_audit(
    df: pd.DataFrame,
    timeframe: str,
    sessions_config: dict[str, dict[str, int]] | None = None,
    max_gaps: int = 200,
) -> dict[str, Any]:
    """Per-year/per-session coverage plus the largest missing-bar gaps.

    Expected bars are the aligned interval timestamps on Mon-Fri (UTC);
    weekend (Sat/Sun) coverage depends on the broker's schedule and cannot be
    inferred, so weekend hours never inflate the missing-bars total. A run of
    missing bars is marked ``spans_weekend`` when a Saturday/Sunday lies in the
    calendar interval between the last present bar before it and the first
    present bar after it.
    """
    sessions_config = sessions_config or {}
    interval = _interval_seconds(timeframe)
    if df.empty:
        return {"present_bars": 0, "missing_bars": 0, "weekend_gaps": 0,
                "coverage": None, "gaps": [], "gap_count": 0,
                "per_year": {}, "per_session": {}}
    ts = pd.to_datetime(df["timestamp_utc"], unit="s", utc=True).sort_values()
    start_ts = int(ts.iloc[0].timestamp())
    end_ts = int(ts.iloc[-1].timestamp())
    present = set(int(t.timestamp()) for t in ts)
    aligned = _aligned_bars(start_ts, end_ts, interval)
    aligned_ts = [int(t.timestamp()) for t in aligned]

    weekday_expected = _expected_bars_per_year(start_ts, end_ts, interval)
    weekday_present: dict[int, int] = {}
    missing_by_year: dict[int, list[int]] = {}
    session_present: dict[str, int] = {}
    session_missing: dict[str, int] = {}
    for bar in aligned_ts:
        t = pd.Timestamp(bar, unit="s", tz="UTC")
        if t.weekday() >= 5:
            continue
        year = t.year
        session = _session_of(t, sessions_config)
        if bar in present:
            weekday_present[year] = weekday_present.get(year, 0) + 1
            session_present[session] = session_present.get(session, 0) + 1
        else:
            missing_by_year.setdefault(year, []).append(bar)
            session_missing[session] = session_missing.get(session, 0) + 1

    per_year = {}
    for year in sorted(set(weekday_expected) | set(weekday_present)):
        expected = weekday_expected.get(year, 0)
        found = weekday_present.get(year, 0)
        per_year[str(year)] = {
            "expected_bars": expected,
            "present_bars": found,
            "missing_bars": max(0, expected - found),
            "coverage": round(found / expected, 6) if expected else None,
        }

    per_session = {}
    for session in sorted(set(session_present) | set(session_missing)):
        found = session_present.get(session, 0)
        missing = session_missing.get(session, 0)
        per_session[session] = {
            "present_bars": found,
            "missing_bars": missing,
            "coverage": round(found / (found + missing), 6) if (found + missing) else None,
        }

    # Contiguous missing-bar runs; mark runs whose calendar interval contains a
    # Saturday or Sunday (between the surrounding present bars).
    gaps: list[dict[str, Any]] = []
    present_sorted = sorted(present)
    missing_sorted = sorted(bar for year in missing_by_year.values() for bar in year)
    run: list[int] = []
    for bar in missing_sorted:
        if run and bar - run[-1] > interval:
            _close_gap_run(run, interval, gaps, present_sorted)
            run = []
        run.append(bar)
    if run:
        _close_gap_run(run, interval, gaps, present_sorted)
    weekday_gaps = [g for g in gaps if not g["spans_weekend"]]
    weekend_gap_count = sum(1 for g in gaps if g["spans_weekend"])
    total_expected = sum(weekday_expected.values()) if weekday_expected else 0
    total_present = sum(weekday_present.values())
    return {
        "present_bars": int(total_present),
        "missing_bars": max(0, total_expected - total_present),
        "weekend_gaps": int(weekend_gap_count),
        "coverage": round(total_present / total_expected, 6) if total_expected else None,
        "gaps": weekday_gaps[:max_gaps],
        "gap_count": len(weekday_gaps),
        "per_year": per_year,
        "per_session": per_session,
    }


def _close_gap_run(
    run: list[int],
    interval: int,
    gaps: list[dict],
    present_sorted: list[int],
) -> None:
    start = run[0]
    end = run[-1]
    prev_bar = max((b for b in present_sorted if b < start), default=None)
    next_bar = min((b for b in present_sorted if b > end), default=None)
    spans_weekend = False
    if prev_bar is not None or next_bar is not None:
        scan_start = prev_bar if prev_bar is not None else start
        scan_end = next_bar if next_bar is not None else end
        probe = pd.Timestamp(scan_start, unit="s", tz="UTC")
        limit = pd.Timestamp(scan_end, unit="s", tz="UTC")
        while probe <= limit:
            if probe.weekday() >= 5:
                spans_weekend = True
                break
            probe += pd.Timedelta(hours=12)  # weekday can change only on a 12h step
        # Exact boundary check for short spans.
        for raw in (scan_start, scan_end, start, end):
            if pd.Timestamp(raw, unit="s", tz="UTC").weekday() >= 5:
                spans_weekend = True
    gaps.append({
        "start_ts_utc": start,
        "end_ts_utc": end,
        "missing_bars": len(run),
        "duration_minutes": int((end - start + interval) / 60),
        "spans_weekend": bool(spans_weekend),
    })


def compute_data_hash(df: pd.DataFrame) -> str:
    """sha256 over the canonical sorted export rows (content fingerprint)."""
    ordered = df.sort_values("timestamp_utc")
    payload = ordered.to_csv(
        index=False, columns=["timestamp_utc", "open", "high", "low", "close",
                              "volume", "spread", "real_volume"],
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_sha256(path: str) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_provenance_manifest(
    db_path: str,
    timeframe: str,
    asset_key: str,
    *,
    broker: str,
    broker_symbol: str | None = None,
    terminal_company_hash: str | None = None,
    terminal_build: str | None = None,
    terminal_data_path_hash: str | None = None,
    server_time_offset_hours: float = 0.0,
    sessions_config: dict[str, dict[str, int]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the immutable manifest for one asset/timeframe export."""
    df = read_candles(db_path, timeframe, asset_key)
    if df.empty:
        raise ValueError(f"no {asset_key} {timeframe} candles in {db_path}")
    start_ts = int(df["timestamp_utc"].min())
    end_ts = int(df["timestamp_utc"].max())
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "asset_key": asset_key,
        "broker_symbol": broker_symbol or asset_key,
        "canonical_symbol": asset_key,
        "broker": broker,
        "terminal_company_hash": terminal_company_hash,
        "terminal_build": terminal_build,
        "terminal_data_path_hash": terminal_data_path_hash,
        "server_time_offset_hours": float(server_time_offset_hours),
        "timeframe": timeframe.upper(),
        "interval_seconds": _interval_seconds(timeframe),
        "source_window_utc": {"start_ts": start_ts, "end_ts": end_ts},
        "export_time_utc": datetime.now(timezone.utc).isoformat(),
        "db_file_sha256": file_sha256(db_path),
        "candle_count": int(len(df)),
        "gap_audit": gap_audit(df, timeframe, sessions_config),
        "data_hash": compute_data_hash(df),
    }
    if extra:
        manifest["extra"] = extra
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return manifest


def write_provenance_manifest(path: str, manifest: dict[str, Any]) -> str:
    """Atomic write; an existing path may only be overwritten byte-for-byte."""
    payload = json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() != payload:
                raise RuntimeError(
                    f"provenance manifest already exists with different content: {path}"
                )
        return manifest["manifest_hash"]
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(tmp_path, path)
    return manifest["manifest_hash"]


def load_provenance_manifest(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"unsupported provenance manifest schema: {manifest.get('schema_version')}")
    return manifest


def verify_provenance_manifest(
    db_path: str,
    timeframe: str,
    asset_key: str,
    manifest: dict[str, Any] | str,
    *,
    require: bool = True,
) -> dict[str, Any]:
    """Recompute the content fingerprint and fail closed on any mismatch.

    ``manifest`` may be a path or an already-loaded dict. Broker/terminal
    identity fields are informational; content fingerprint, source window and
    candle count are enforced.
    """
    loaded = load_provenance_manifest(manifest) if isinstance(manifest, str) else manifest
    if loaded.get("asset_key") != asset_key:
        raise RuntimeError(
            f"provenance manifest asset_key={loaded.get('asset_key')!r} "
            f"does not match requested {asset_key!r}"
        )
    if (loaded.get("timeframe") or "").upper() != timeframe.upper():
        raise RuntimeError(
            f"provenance manifest timeframe={loaded.get('timeframe')!r} "
            f"does not match requested {timeframe!r}"
        )
    df = read_candles(db_path, timeframe, asset_key)
    if df.empty:
        raise RuntimeError(f"no {asset_key} {timeframe} candles in {db_path}")
    recomputed_hash = compute_data_hash(df)
    if recomputed_hash != loaded.get("data_hash"):
        raise RuntimeError(
            f"provenance data_hash mismatch for {asset_key} {timeframe}: "
            f"manifest {loaded.get('data_hash')} != recomputed {recomputed_hash}. "
            f"Source data changed after the manifest was frozen."
        )
    window = loaded.get("source_window_utc") or {}
    if window.get("start_ts") != int(df["timestamp_utc"].min()) or \
            window.get("end_ts") != int(df["timestamp_utc"].max()):
        raise RuntimeError(
            f"provenance source window mismatch for {asset_key} {timeframe}: "
            f"manifest {window} != data [{int(df['timestamp_utc'].min())}, "
            f"{int(df['timestamp_utc'].max())}]"
        )
    if loaded.get("candle_count") != int(len(df)):
        raise RuntimeError(
            f"provenance candle_count mismatch for {asset_key} {timeframe}: "
            f"manifest {loaded.get('candle_count')} != data {len(df)}"
        )
    return {"verified": True, "asset_key": asset_key, "timeframe": timeframe,
            "data_hash": recomputed_hash}


def resolve_manifest_path(validation: dict, timeframe: str, asset_key: str) -> str | None:
    """Resolve the frozen manifest path for one (asset, timeframe).

    * ``validation.provenance_manifest_path`` pointing at a FILE is used as-is
      (single-asset setups, the original contract);
    * pointing at a DIRECTORY selects ``<ASSET>_<TF>_fxpro.json`` inside it
      (the naming produced by scripts/rebuild_provenance_manifests.py); the
      legacy lowercase ``<asset>_<tf>.json`` form is also accepted. A missing
      per-asset manifest is a hard error so a run can never silently skip
      verification.
    """
    path = (validation or {}).get("provenance_manifest_path")
    if not path:
        return None
    if os.path.isdir(path):
        candidates = (
            f"{asset_key}_{timeframe.upper()}_fxpro.json",
            f"{asset_key.lower()}_{timeframe.lower()}.json",
        )
        for candidate in candidates:
            full = os.path.join(path, candidate)
            if os.path.isfile(full):
                return full
        raise RuntimeError(
            f"provenance manifest required for {asset_key} {timeframe.upper()} but "
            f"not found in {path!r}; expected {candidates[0]}"
        )
    return path


def provenance_gate(
    cfg: dict,
    db_path: str,
    timeframe: str,
    asset_key: str,
    *,
    require: bool | None = None,
) -> dict[str, Any]:
    """Config-gated entry point: raise when provenance is required but absent
    or mismatched. ``require=None`` reads ``validation.require_provenance_manifest``
    (default off so existing workflows keep running untouched)."""
    validation = (cfg or {}).get("validation", {}) or {}
    if require is None:
        require = bool(validation.get("require_provenance_manifest", False))
    if not require:
        return {"verified": False, "required": False, "reason": "not required"}
    manifest_path = resolve_manifest_path(validation, timeframe, asset_key)
    if not manifest_path or not os.path.isfile(manifest_path):
        raise RuntimeError(
            f"provenance manifest required by validation config but not found: "
            f"{manifest_path!r} (set validation.provenance_manifest_path "
            f"to a file or a directory of <ASSET>_<TF>_fxpro.json manifests)"
        )
    result = verify_provenance_manifest(db_path, timeframe, asset_key, manifest_path)
    result["required"] = True
    result["manifest_path"] = manifest_path
    return result
