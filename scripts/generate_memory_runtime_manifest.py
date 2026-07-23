#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath


EVEROS_VERSION = "1.1.3"
PYTHON_VERSION = "3.12.12"
LOCK_SHA256 = "37ab1606edf1a6299a9d52b5a99d288a81218a5a0b1eb89d60644f3ace4255eb"
UV_VERSION = "0.9.18"
ARCHIVE_PREFIX = f"memory-runtime-{EVEROS_VERSION}-"
ARCHIVE_SUFFIX = ".tar.gz"
BIN_PATH = "bin/python"
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
EXPECTED_PLATFORMS = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_from_archive(path: Path) -> str:
    name = path.name
    if not name.startswith(ARCHIVE_PREFIX) or not name.endswith(ARCHIVE_SUFFIX):
        raise ValueError(f"Unexpected Memory Runtime archive name: {name}")
    return name[len(ARCHIVE_PREFIX) : -len(ARCHIVE_SUFFIX)]


def _binary_sha256(archive: Path) -> str:
    with tarfile.open(archive, "r:gz") as package:
        for candidate in package.getmembers():
            path = PurePosixPath(candidate.name)
            windows_path = PureWindowsPath(candidate.name)
            if (
                not (candidate.isfile() or candidate.isdir())
                or path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or ".." in path.parts
                or ".." in windows_path.parts
            ):
                raise SystemExit(f"Memory Runtime archive member is unsafe: {archive.name}:{candidate.name}")
        try:
            member = package.getmember(BIN_PATH)
        except KeyError as exc:
            raise SystemExit(f"Memory Runtime archive is missing {BIN_PATH}: {archive.name}") from exc
        if not member.isfile():
            raise SystemExit(f"Memory Runtime binary is not a regular file: {archive.name}:{BIN_PATH}")
        binary = package.extractfile(member)
        if binary is None:
            raise SystemExit(f"Memory Runtime binary cannot be read: {archive.name}:{BIN_PATH}")
        digest = hashlib.sha256()
        for chunk in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def build_manifest(*, archive_dir: Path, tag: str, repo: str, output: Path) -> dict:
    tag = tag.strip()
    repo = repo.strip().strip("/")
    if not tag or not repo or "/" not in repo:
        raise SystemExit("A release tag and owner/repository are required")

    archives: dict[str, dict[str, object]] = {}
    base_url = f"https://github.com/{repo}/releases/download/{tag}"
    for archive in sorted(archive_dir.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}")):
        platform = _platform_from_archive(archive)
        if platform not in EXPECTED_PLATFORMS:
            raise SystemExit(f"Unsupported Memory Runtime platform archive: {platform}")
        metadata_path = archive.with_suffix("").with_suffix(".json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Missing or invalid Memory Runtime build metadata: {metadata_path.name}") from exc
        archive_sha256 = _sha256(archive)
        binary_sha256 = _binary_sha256(archive)
        size = archive.stat().st_size
        expected_metadata = {
            "platform": platform,
            "everos_version": EVEROS_VERSION,
            "python_version": PYTHON_VERSION,
            "lock_sha256": LOCK_SHA256,
            "uv_version": UV_VERSION,
            "name": archive.name,
            "sha256": archive_sha256,
            "binary_sha256": binary_sha256,
            "size": size,
            "bin_path": BIN_PATH,
        }
        if metadata != expected_metadata:
            raise SystemExit(f"Memory Runtime build metadata mismatch: {metadata_path.name}")
        if size > MAX_ARCHIVE_BYTES:
            raise SystemExit(f"Memory Runtime archive exceeds the 1 GiB release limit: {archive.name}")
        archives[platform] = {
            "name": archive.name,
            "url": f"{base_url}/{archive.name}",
            "sha256": archive_sha256,
            "binary_sha256": binary_sha256,
            "size": size,
            "bin_path": BIN_PATH,
        }

    missing = sorted(EXPECTED_PLATFORMS - set(archives))
    if missing:
        raise SystemExit("Missing Memory Runtime archives: " + ", ".join(missing))

    manifest = {
        "schema_version": 1,
        "everos_version": EVEROS_VERSION,
        "python_version": PYTHON_VERSION,
        "lock_sha256": LOCK_SHA256,
        "lock_id": f"uv-lock-sha256:{LOCK_SHA256}",
        "uv_version": UV_VERSION,
        "source": "everos",
        "source_url": f"https://pypi.org/project/everos/{EVEROS_VERSION}/",
        "release_state": "published",
        "release_tag": tag,
        "provider_root_format": f"everos-{EVEROS_VERSION}",
        "compatible_provider_root_formats": [],
        "archives": archives,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the Avibe Memory Runtime release manifest.")
    parser.add_argument("--archive-dir", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = build_manifest(
        archive_dir=args.archive_dir,
        tag=args.tag,
        repo=args.repo,
        output=args.output,
    )
    print(json.dumps({"ok": True, "platforms": sorted(manifest["archives"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
