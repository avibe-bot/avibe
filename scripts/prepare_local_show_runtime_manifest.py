#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from show_runtime_manifest_asset import prepare_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "vibe" / "show_runtime_manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a verified Show Runtime manifest for a local Avibe wheel build."
    )
    parser.add_argument(
        "--release-tag",
        help="Official avibe-bot/avibe release tag to inherit. Defaults to the latest stable release.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Manifest destination used by the Avibe wheel build.",
    )
    args = parser.parse_args()
    manifest = prepare_manifest(args.output, release_tag=args.release_tag)
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output),
                "runtime_version": manifest["runtime_version"],
                "platforms": sorted(manifest["archives"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
