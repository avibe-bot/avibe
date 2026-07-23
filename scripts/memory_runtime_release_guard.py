#!/usr/bin/env python3
"""Verify and materialize manifest-pinned Memory Runtime release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

RELEASE_DOWNLOAD_ROOT = "https://github.com/avibe-bot/avibe/releases/download"
EXPECTED_PYTHON_VERSION = "3.12.12"
EXPECTED_LOCK_SHA256 = "37ab1606edf1a6299a9d52b5a99d288a81218a5a0b1eb89d60644f3ace4255eb"
EXPECTED_UV_VERSION = "0.9.18"
EXPECTED_PLATFORMS = frozenset({"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64"})
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024


class ReleaseGuardError(RuntimeError):
    """Raised when published Memory Runtime bytes do not match their manifest."""


@dataclass(frozen=True)
class ArchiveSpec:
    platform: str
    name: str
    url: str
    sha256: str
    binary_sha256: str
    size: int
    bin_path: str


@dataclass(frozen=True)
class ReleaseSpec:
    manifest_bytes: bytes
    release_tag: str
    archives: tuple[ArchiveSpec, ...]

    @property
    def expected_asset_names(self) -> set[str]:
        return {"memory-runtime-manifest.json", *(archive.name for archive in self.archives)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(payload: dict, key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ReleaseGuardError(f"{context}.{key} must be a non-empty string")
    return value


def load_release_spec(manifest_path: Path) -> ReleaseSpec:
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGuardError(f"cannot read Memory Runtime manifest: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ReleaseGuardError("Memory Runtime manifest schema_version must be 1")
    if payload.get("release_state") != "published" or payload.get("everos_version") != "1.1.3":
        raise ReleaseGuardError("Memory Runtime manifest must describe published EverOS 1.1.3")
    if (
        payload.get("python_version") != EXPECTED_PYTHON_VERSION
        or payload.get("lock_sha256") != EXPECTED_LOCK_SHA256
        or payload.get("lock_id") != f"uv-lock-sha256:{EXPECTED_LOCK_SHA256}"
        or payload.get("uv_version") != EXPECTED_UV_VERSION
    ):
        raise ReleaseGuardError("Memory Runtime manifest provenance is invalid")

    release_tag = _required_string(payload, "release_tag", "manifest")
    release_root = f"{RELEASE_DOWNLOAD_ROOT}/{release_tag}"
    raw_archives = payload.get("archives")
    if not isinstance(raw_archives, dict) or set(raw_archives) != EXPECTED_PLATFORMS:
        raise ReleaseGuardError("Memory Runtime manifest platform set is invalid")
    archives: list[ArchiveSpec] = []
    for platform, raw in sorted(raw_archives.items()):
        if not isinstance(raw, dict):
            raise ReleaseGuardError(f"archives.{platform} must be an object")
        context = f"archives.{platform}"
        name = _required_string(raw, "name", context)
        url = _required_string(raw, "url", context)
        sha256 = _required_string(raw, "sha256", context)
        binary_sha256 = _required_string(raw, "binary_sha256", context)
        bin_path = _required_string(raw, "bin_path", context)
        size = raw.get("size")
        if Path(name).name != name or url != f"{release_root}/{name}":
            raise ReleaseGuardError(f"{context} is outside the pinned release")
        if (
            len(sha256) != 64
            or len(binary_sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256 + binary_sha256)
        ):
            raise ReleaseGuardError(f"{context} has an invalid digest")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ReleaseGuardError(f"{context}.size is invalid")
        if Path(bin_path).is_absolute() or ".." in Path(bin_path).parts:
            raise ReleaseGuardError(f"{context}.bin_path is unsafe")
        archives.append(ArchiveSpec(platform, name, url, sha256, binary_sha256, size, bin_path))
    return ReleaseSpec(manifest_bytes=manifest_bytes, release_tag=release_tag, archives=tuple(archives))


def verify_release_assets(manifest_path: Path, asset_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    if not asset_dir.is_dir():
        raise ReleaseGuardError("Memory Runtime asset directory is missing")
    entries = list(asset_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ReleaseGuardError("Memory Runtime asset directory contains unsafe entries")
    actual = {path.name for path in entries}
    if actual != spec.expected_asset_names:
        raise ReleaseGuardError(
            f"Memory Runtime asset set mismatch: missing={sorted(spec.expected_asset_names - actual)}, "
            f"unexpected={sorted(actual - spec.expected_asset_names)}"
        )
    if (asset_dir / "memory-runtime-manifest.json").read_bytes() != spec.manifest_bytes:
        raise ReleaseGuardError("published Memory Runtime manifest differs from the pinned manifest")
    for archive in spec.archives:
        path = asset_dir / archive.name
        if path.stat().st_size != archive.size or _sha256(path) != archive.sha256:
            raise ReleaseGuardError(f"Memory Runtime archive integrity mismatch: {archive.name}")
        try:
            with tarfile.open(path, "r:gz") as bundle:
                member = bundle.getmember(archive.bin_path)
                binary = bundle.extractfile(member)
                digest = hashlib.sha256(binary.read()).hexdigest() if binary is not None and member.isfile() else ""
        except (KeyError, OSError, tarfile.TarError) as exc:
            raise ReleaseGuardError(f"invalid Memory Runtime archive: {archive.name}") from exc
        if digest != archive.binary_sha256:
            raise ReleaseGuardError(f"Memory Runtime binary integrity mismatch: {archive.name}")
    return spec


def _download(url: str, destination: Path, attempts: int = 3) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "avibe-memory-runtime-release-guard/1"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            return
        except (OSError, urllib.error.URLError) as exc:
            if attempt == attempts:
                raise ReleaseGuardError(f"release asset download failed: {url}: {exc}") from exc
            time.sleep(float(attempt))


def fetch_release_assets(manifest_path: Path, output_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        (temporary / "memory-runtime-manifest.json").write_bytes(spec.manifest_bytes)
        for archive in spec.archives:
            _download(archive.url, temporary / archive.name)
        verify_release_assets(manifest_path, temporary)
        if output_dir.exists():
            if output_dir.is_dir():
                shutil.rmtree(output_dir)
            else:
                output_dir.unlink()
        temporary.replace(output_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch")
    fetch.add_argument("--output-dir", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--asset-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        spec = (
            fetch_release_assets(args.manifest, args.output_dir)
            if args.command == "fetch"
            else verify_release_assets(args.manifest, args.asset_dir)
        )
    except ReleaseGuardError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "release_tag": spec.release_tag, "asset_count": len(spec.expected_asset_names)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
