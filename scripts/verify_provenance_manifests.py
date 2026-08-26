"""Verify EVERY frozen provenance manifest against the live DB in one call.

Scans the manifest directory (``validation.provenance_manifest_path`` from
config, default ``config/provenance``) and for each ``<ASSET>_<TF>_fxpro.json``
performs the same fail-closed checks the runtime gate (``provenance_gate``)
does before every train/backtest:

  * schema check (``load_provenance_manifest``);
  * manifest self-hash (stored ``manifest_hash`` vs recomputed over the file
    content — catches tampering the DB check cannot see);
  * content verification against the DB via
    ``data.provenance.verify_provenance_manifest`` — recomputed ``data_hash``,
    ``source_window_utc`` and ``candle_count`` must ALL match the frozen
    manifest, otherwise the manifest is stale (source data changed after the
    export was frozen) and the run that produced it is not reproducible.

One manifest failing never stops the sweep: every file gets a row, the CSV
report is always written, and the process exits 1 if ANY manifest failed — so
CI fails closed instead of silently skipping a stale manifest.

Usage:
    python -m scripts.verify_provenance_manifests [--db data/market_data_mt5.sqlite]
        [--manifest-dir config/provenance] [--out logs/provenance_manifest_audit.csv]
        [--allow-empty]

Exit code 0 = every manifest verified; 1 = at least one failure, OR no
manifests found unless ``--allow-empty`` (a sweep that finds nothing must not
report green in CI). Read-only — never modifies the DB or the manifests.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from data.provenance import (
    compute_data_hash,
    load_provenance_manifest,
    verify_provenance_manifest,
)
from data.storage import read_candles

DEFAULT_MANIFEST_DIR = "config/provenance"
DEFAULT_OUT_CSV = os.path.join("logs", "provenance_manifest_audit.csv")

CSV_COLUMNS = [
    "manifest",
    "asset_key",
    "timeframe",
    "schema_version",
    "export_time_utc",
    "candle_count",
    "source_window_utc",
    "stored_data_hash",
    "recomputed_data_hash",
    "data_hash_match",
    "window_match",
    "count_match",
    "manifest_hash_ok",
    "db_file_sha256_match",
    "verified",
    "reason",
]


def _recompute_manifest_hash(manifest: dict) -> str:
    """The manifest carries its own sha256 over the compact sorted JSON; a
    mismatch means the file was edited after freezing (no DB involved).

    ``build_provenance_manifest`` computes the hash over the dict BEFORE
    inserting the ``manifest_hash`` key itself, so the recompute must exclude
    that key too — otherwise a freshly frozen manifest would always "fail".
    """
    payload = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: str) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_defaults(path: str) -> dict:
    return {
        "manifest": path,
        "asset_key": "",
        "timeframe": "",
        "schema_version": "",
        "export_time_utc": "",
        "candle_count": "",
        "source_window_utc": "",
        "stored_data_hash": "",
        "recomputed_data_hash": "",
        "data_hash_match": "",
        "window_match": "",
        "count_match": "",
        "manifest_hash_ok": "",
        "db_file_sha256_match": "",
        "verified": False,
        "reason": "",
    }


def verify_one(db_path: str, manifest_path: str) -> dict:
    """Verify one manifest file; never raises — always returns a CSV row."""
    row = _row_defaults(manifest_path)

    try:
        manifest = load_provenance_manifest(manifest_path)
    except Exception as exc:
        row["reason"] = f"load/schema failed: {exc}"
        return row

    row["asset_key"] = manifest.get("asset_key", "")
    row["timeframe"] = manifest.get("timeframe", "")
    row["schema_version"] = manifest.get("schema_version", "")
    row["export_time_utc"] = manifest.get("export_time_utc", "")
    row["candle_count"] = manifest.get("candle_count", "")
    window = manifest.get("source_window_utc") or {}
    if window.get("start_ts") is not None and window.get("end_ts") is not None:
        row["source_window_utc"] = f"{window['start_ts']}..{window['end_ts']}"
    row["stored_data_hash"] = manifest.get("data_hash", "")

    # Self-hash over the frozen file content (tamper check, no DB read).
    row["manifest_hash_ok"] = (
        _recompute_manifest_hash(manifest) == manifest.get("manifest_hash")
    )

    # Content breakdown from one DB read, then the authoritative gate.
    try:
        df = read_candles(db_path, row["timeframe"], row["asset_key"])
    except Exception as exc:
        row["reason"] = f"db read failed: {exc}"
        return row
    if df.empty:
        row["reason"] = (
            f"no candles for {row['asset_key']} {row['timeframe']} in {db_path}"
        )
        return row

    recomputed = compute_data_hash(df)
    row["recomputed_data_hash"] = recomputed
    row["data_hash_match"] = recomputed == manifest.get("data_hash")
    row["window_match"] = (
        window.get("start_ts") == int(df["timestamp_utc"].min())
        and window.get("end_ts") == int(df["timestamp_utc"].max())
    )
    row["count_match"] = manifest.get("candle_count") == int(len(df))

    try:
        verify_provenance_manifest(db_path, row["timeframe"], row["asset_key"], manifest)
        row["verified"] = True
        row["reason"] = "ok"
    except Exception as exc:
        row["verified"] = False
        row["reason"] = str(exc)

    # The manifest_hash is the freeze seal over the WHOLE file: even a
    # cosmetic edit (e.g. export_time_utc) that the DB checks cannot see
    # breaks the byte-level immutability contract -> fail the sweep.
    if not row["manifest_hash_ok"]:
        row["verified"] = False
        row["reason"] = (
            "manifest self-hash mismatch (file modified after freeze); "
            + row["reason"]
        )

    # Informational only: the DB FILE sha256 changes on any legitimate append
    # or sqlite checkpoint, so it is reported but never fails the sweep. The
    # content-level data_hash is the authoritative reproducibility check.
    db_file_sha = _file_sha256(db_path)
    stored_db_sha = manifest.get("db_file_sha256")
    if stored_db_sha and db_file_sha:
        row["db_file_sha256_match"] = db_file_sha == stored_db_sha
    else:
        row["db_file_sha256_match"] = "n/a"

    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None,
                        help="path to the market-data SQLite DB "
                             "(default: config general.db_path)")
    parser.add_argument("--manifest-dir", default=None,
                        help="directory of <ASSET>_<TF>_fxpro.json manifests "
                             "(default: config validation.provenance_manifest_path)")
    parser.add_argument("--out", default=DEFAULT_OUT_CSV,
                        help=f"CSV report path (default: {DEFAULT_OUT_CSV})")
    parser.add_argument("--allow-empty", action="store_true",
                        help="exit 0 when no manifests are found "
                             "(default: fail, so CI cannot report green on nothing)")
    args = parser.parse_args()

    cfg = load_config()
    db_path = args.db or cfg.get("general", {}).get(
        "db_path", "data/market_data_mt5.sqlite")
    manifest_dir = args.manifest_dir or (
        (cfg.get("validation", {}) or {}).get("provenance_manifest_path")
        or DEFAULT_MANIFEST_DIR
    )

    if not os.path.isdir(manifest_dir):
        print(f"MANIFEST DIR not found: {manifest_dir}")
        return 1
    manifest_paths = sorted(
        os.path.join(manifest_dir, name)
        for name in os.listdir(manifest_dir)
        if name.lower().endswith(".json")
    )
    if not manifest_paths:
        print(f"no manifests (*.json) found in {manifest_dir}")
        return 0 if args.allow_empty else 1

    rows = [verify_one(db_path, path) for path in manifest_paths]

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    verified = sum(1 for r in rows if r["verified"])
    total = len(rows)
    print(f"provenance manifests: {verified}/{total} verified")
    print(f"report: {args.out}")
    for r in rows:
        status = "OK  " if r["verified"] else "FAIL"
        print(f"  [{status}] {r['asset_key']} {r['timeframe']} "
              f"{os.path.basename(r['manifest'])}: {r['reason']}")

    return 0 if verified == total else 1


if __name__ == "__main__":
    sys.exit(main())
