#!/usr/bin/env python3
"""Fetch and verify the vendored NeuroBook sources (TZ_BOOKS task T-01).

Downloads the pinned version of the book's MQL5 neural-network library
("Neural Networks for Algorithmic Trading with MQL5", Dmitriy Gizlyk /
MetaQuotes) into ``mql5/NeuroBook/vendor/`` and verifies it against the
committed manifest ``NEUROBOOK_MANIFEST.json`` (file inventory + sizes).

The vendored bytes themselves are intentionally NOT committed to git
(licensing + repository size); the committed manifest is the version pin.
After fetching, copy the tree into the terminal:

    %APPDATA%\\MetaQuotes\\Terminal\\<INSTANCE>\\MQL5\\
        Include/NeuroNetworksBook/...
        Experts/NeuroNetworksBook/...
        Scripts/NeuroNetworksBook/...

or open the shared project ``\\MQL5\\Shared Projects\\NeuroBook`` directly
in MetaEditor (MQL5 Storage) at the pinned forge commit.

Usage:
    python mql5/NeuroBook/fetch_neurobook.py            # download ZIP + verify
    python mql5/NeuroBook/fetch_neurobook.py --verify   # verify only
    python mql5/NeuroBook/fetch_neurobook.py --from-forge  # git clone (needs git)

Exit code 0 = vendor directory matches the manifest.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(HERE, "NEUROBOOK_MANIFEST.json")
VENDOR_DIR = os.path.join(HERE, "vendor")
STAMP_PATH = os.path.join(VENDOR_DIR, ".neurobook.stamp")


def load_manifest() -> dict:
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _match_size(expected_kb: float, actual_bytes: int, tolerance: float) -> bool:
    expected_bytes = expected_kb * 1024.0
    return abs(actual_bytes - expected_bytes) <= expected_bytes * tolerance


def verify(manifest: dict) -> tuple[bool, list[str]]:
    """Check the vendor tree against the manifest inventory."""
    problems: list[str] = []
    tolerance = float(manifest.get("size_tolerance", 0.25))
    for entry in manifest["files"]:
        rel = entry["path"]
        path = os.path.join(VENDOR_DIR, rel)
        if not os.path.isfile(path):
            problems.append(f"MISSING {rel}")
            continue
        actual = os.path.getsize(path)
        if not _match_size(float(entry["size_kb"]), actual, tolerance):
            problems.append(
                f"SIZE MISMATCH {rel}: expected ~{entry['size_kb']} KiB, got {actual / 1024:.2f} KiB")
    # Unexpected extra source files are tolerated (publications add files);
    # only report them so drift is visible.
    expected = {e["path"] for e in manifest["files"]}
    for root, _dirs, files in os.walk(VENDOR_DIR):
        for name in files:
            if name == ".neurobook.stamp":
                continue
            rel = os.path.relpath(os.path.join(root, name), VENDOR_DIR).replace(os.sep, "/")
            if rel not in expected:
                problems.append(f"UNLISTED {rel}")
    return (len(problems) == 0), problems


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def fetch_zip(manifest: dict) -> tuple[bool, list[str]]:
    """Download the CodeBase ZIP archive and extract it into vendor/."""
    zip_url = manifest["version_pin"]["codebase_zip_url"]
    if not zip_url:
        print("manifest has no codebase_zip_url", file=sys.stderr)
        return False, ["no zip url"]
    os.makedirs(VENDOR_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "neurobook.zip")
        print(f"downloading {zip_url} ...")
        _download(zip_url, zip_path)
        # The codebase ZIP stores files flat with codebase-unique names OR
        # keeps the documented subfolders; map by basename into the manifest
        # layout either way.
        wanted_by_basename = {os.path.basename(e["path"]): e["path"] for e in manifest["files"]}
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            for name in names:
                base = os.path.basename(name)
                if base not in wanted_by_basename:
                    continue
                target = os.path.join(VENDOR_DIR, wanted_by_basename[base])
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    with open(STAMP_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest["version_pin"], fh, indent=2)
    return verify(manifest)


def fetch_forge(manifest: dict) -> tuple[bool, list[str]]:
    """Clone the pinned commit from the MQL5 Algo Forge git mirror."""
    pin = manifest["version_pin"]
    url, commit = pin["forge_url"] + ".git", pin["forge_commit"]
    if shutil.which("git") is None:
        print("git is not available", file=sys.stderr)
        return False, ["git missing"]
    if os.path.isdir(VENDOR_DIR):
        shutil.rmtree(VENDOR_DIR)
    subprocess.run(["git", "clone", "--quiet", url, VENDOR_DIR], check=True)
    subprocess.run(["git", "-C", VENDOR_DIR, "checkout", "--quiet", commit], check=True)
    # keep only documented tree layout, drop forge-specific files
    return verify(manifest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true", help="verify only, no download")
    ap.add_argument("--from-forge", action="store_true",
                    help="git clone the pinned forge commit instead of the ZIP")
    args = ap.parse_args(argv)

    manifest = load_manifest()
    if args.verify:
        ok, problems = verify(manifest)
    elif args.from_forge:
        ok, problems = fetch_forge(manifest)
    else:
        ok, problems = fetch_zip(manifest)

    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
    print("NeuroBook vendor: " + ("OK (matches pinned manifest)" if ok else "MISMATCH"))
    print("Next: copy mql5/NeuroBook/vendor/{Include,Experts,Scripts} into the "
          "terminal MQL5 folder, or open \\\\MQL5\\\\Shared Projects\\\\NeuroBook "
          f"at commit {manifest['version_pin']['forge_commit'][:8]} in MetaEditor.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
