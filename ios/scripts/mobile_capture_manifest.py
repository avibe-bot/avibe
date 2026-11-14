#!/usr/bin/env python3
"""Record the exact UI bytes and 24 expected simulator capture names."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

STATES = ("default", "offline-open", "open-failed", "stale", "account", "first-run")


def capture_manifest(bundle: Path, sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected an exact lowercase 40-character source SHA")
    if not (bundle / "mobile.html").is_file() or not (bundle / "assets").is_dir():
        raise ValueError("Expected the real built mobile.html and assets directory")
    files = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink():
            raise ValueError("Capture bundle must not contain symbolic links")
        if path.is_file():
            files[path.relative_to(bundle).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "source_sha": sha,
        "device": "iPhone 17 Pro",
        "runtime": "iOS 26.5",
        "bundle_sha256": files,
        "expected_attachments": [
            f"mobile-{state}-{language}-{theme}"
            for language in ("en", "zh")
            for theme in ("light", "dark")
            for state in STATES
        ],
        "data": "Synthetic example.invalid account and instances; no credentials",
        "open_failed": "Real native invalid_instance rejection of a synthetic non-HTTPS catalog URL",
        "clipboard": "No native or fallback clipboard calls are made by these scenarios",
        "masking": "No secret-bearing inputs are populated or reachable in the captured states",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = capture_manifest(args.bundle, args.sha)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
