"""Audit every model bundle under output/models against its stored self-hash.

For each ``*.joblib`` file (recursive, so backups/candidates/retrain dirs are
covered too):

  * NEW bundle (dict with ``model`` + ``feature_cols`` [+ ``metadata``]):
      - ``metadata.model_hash`` present -> recompute the deterministic content
        fingerprint (``compute_model_fingerprint``) and compare. Verdict
        ``NEW-OK`` or ``NEW-MISMATCH``.
      - ``metadata.model_hash`` missing/None -> the pipeline treats it via the
        legacy file-sha256 path; the recomputed fingerprint is shown for
        reference. Verdict ``LEGACY``.
  * anything else (raw xgboost / old format / unreadable) -> ``UNRECOGNIZED``
    with the file sha256, so nothing is silently skipped.

Read-only. Writes logs/model_fingerprint_audit.csv.
"""
import hashlib
import os
import sys

import joblib

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from model.trainer import compute_model_fingerprint

MODEL_DIR = os.path.join("output", "models")
OUT_CSV = os.path.join("logs", "model_fingerprint_audit.csv")


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _asset_from_name(path: str) -> str:
    base = os.path.basename(path)
    for a in ("XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD", "ETHUSD"):
        if base.lower().startswith(a.lower()):
            return a.upper()
    return "?"


def verify_file(path: str) -> dict:
    row = {
        "file": path,
        "asset": _asset_from_name(path),
        "size_bytes": os.path.getsize(path),
        "file_sha256": _file_sha256(path),
        "trained_at_utc": None,
        "stored_model_hash": None,
        "recomputed_fingerprint": None,
        "verdict": None,
        "note": "",
    }
    try:
        bundle = joblib.load(path)
    except Exception as exc:
        row["verdict"] = "UNRECOGNIZED"
        row["note"] = f"load failed: {exc!r}"
        return row

    if not isinstance(bundle, dict) or "model" not in bundle:
        row["verdict"] = "UNRECOGNIZED"
        row["note"] = f"not a bundle dict (keys={list(bundle) if isinstance(bundle, dict) else type(bundle).__name__})"
        return row

    meta = bundle.get("metadata") or {}
    row["trained_at_utc"] = meta.get("trained_at_utc")
    stored = meta.get("model_hash")
    row["stored_model_hash"] = stored
    cols = bundle.get("feature_cols")

    if stored is None:
        row["verdict"] = "LEGACY"
        row["note"] = "self-hash absent; pipeline verifies via file sha256"
        if cols is not None:
            try:
                row["recomputed_fingerprint"] = compute_model_fingerprint(bundle["model"], cols)
            except Exception as exc:
                row["note"] += f" | fingerprint failed: {exc!r}"
        return row

    try:
        fp = compute_model_fingerprint(bundle["model"], cols)
    except Exception as exc:
        row["verdict"] = "NEW-MISMATCH"
        row["note"] = f"fingerprint recompute failed: {exc!r}"
        return row
    row["recomputed_fingerprint"] = fp
    if fp == stored:
        row["verdict"] = "NEW-OK"
        row["note"] = "self-hash matches recomputed fingerprint"
    else:
        row["verdict"] = "NEW-MISMATCH"
        row["note"] = "stored self-hash != recomputed fingerprint"
    return row


def main() -> None:
    files = sorted(
        os.path.join(root, name)
        for root, _, names in os.walk(MODEL_DIR)
        for name in names
        if name.endswith(".joblib")
    )
    if not files:
        print(f"no *.joblib under {MODEL_DIR}")
        return

    rows = [verify_file(p) for p in files]

    print(f"{'file':<58} {'asset':<7} {'verdict':<15} {'stored':<12} fp-match")
    print("-" * 120)
    for r in rows:
        fp = r["recomputed_fingerprint"]
        match = "n/a"
        if r["verdict"] == "NEW-OK":
            match = "YES"
        elif r["verdict"] == "NEW-MISMATCH":
            match = "NO !!"
        stored = (r["stored_model_hash"] or "None")[:10]
        print(f"{r['file']:<58} {r['asset']:<7} {r['verdict']:<15} {stored:<12} {match}")
    print("-" * 120)

    from collections import Counter
    counts = Counter(r["verdict"] for r in rows)
    print("verdicts:", dict(counts))

    import pandas as pd
    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT_CSV) or ".", exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
