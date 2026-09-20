#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ARCHIVE_PREFIX = "vibe-show-runtime-node-"
ARCHIVE_SUFFIX = ".tgz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_from_archive(path: Path) -> str:
    name = path.name
    if not name.startswith(ARCHIVE_PREFIX) or not name.endswith(ARCHIVE_SUFFIX):
        raise ValueError(f"Unexpected Show Runtime archive name: {name}")
    return name[len(ARCHIVE_PREFIX) : -len(ARCHIVE_SUFFIX)]


def _read_runtime_ref(value: str | None, patterns: list[str]) -> str:
    refs: set[str] = set()
    if value:
        refs.add(value.strip())
    for pattern in patterns:
        for path in sorted(Path().glob(pattern)):
            text = path.read_text(encoding="utf-8").strip()
            if text:
                refs.add(text)
    refs.discard("")
    if len(refs) != 1:
        raise SystemExit("Expected exactly one Show Runtime ref, found: " + ", ".join(sorted(refs)))
    return next(iter(refs))


def _generated_router(archive_dir: Path, platforms: set[str]) -> bytes:
    """Require the template exported by every exact Runtime archive build."""
    routers = []
    for platform in sorted(platforms):
        path = archive_dir / f"show-router-{platform}.tsx"
        if not path.is_file():
            raise SystemExit(f"Missing Show Runtime router template: {path.name}")
        routers.append(path.read_bytes())
    if len(set(routers)) != 1:
        raise SystemExit("Show Runtime router templates differ across platforms")
    router = routers[0]
    if b"export function SsrRouterProvider" not in router:
        raise SystemExit("Show Runtime router template does not support SSR Markdown subroutes")
    return router


def build_manifest(
    *, archive_dir: Path, tag: str, repo: str, runtime_ref: str, output: Path, router_output: Path | None = None
) -> dict:
    archives: dict[str, dict[str, object]] = {}
    base_url = f"https://github.com/{repo}/releases/download/{tag}"
    for archive in sorted(archive_dir.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}")):
        platform = _platform_from_archive(archive)
        archives[platform] = {
            "name": archive.name,
            "url": f"{base_url}/{archive.name}",
            "sha256": _sha256(archive),
            "size": archive.stat().st_size,
        }

    expected = {
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-x64",
        "win32-arm64",
        "win32-x64",
    }
    missing = sorted(expected - set(archives))
    if missing:
        raise SystemExit("Missing Show Runtime archives: " + ", ".join(missing))

    router = _generated_router(archive_dir, expected) if router_output is not None else None
    manifest = {
        "schema_version": 1,
        "runtime_version": runtime_ref,
        "runtime_source": {
            "repo": "avibe-bot/vibe-show-runtime",
            "ref": runtime_ref,
        },
        "minimum_node": "^20.19.0 || >=22.12.0",
        "archives": archives,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if router_output is not None and router is not None:
        router_output.parent.mkdir(parents=True, exist_ok=True)
        router_output.write_bytes(router)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Vibe Show Runtime manifest from platform archives.")
    parser.add_argument("--archive-dir", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True)
    runtime_ref_group = parser.add_mutually_exclusive_group(required=True)
    runtime_ref_group.add_argument("--runtime-ref")
    runtime_ref_group.add_argument(
        "--runtime-ref-file",
        action="append",
        default=[],
        help="Glob pattern for files containing the Show Runtime commit used to build the archives.",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--router-output", required=True, type=Path)
    args = parser.parse_args()
    runtime_ref = _read_runtime_ref(args.runtime_ref, args.runtime_ref_file)
    manifest = build_manifest(
        archive_dir=args.archive_dir,
        tag=args.tag,
        repo=args.repo,
        runtime_ref=runtime_ref,
        output=args.output,
        router_output=args.router_output,
    )
    print(json.dumps({"ok": True, "platforms": sorted(manifest["archives"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
