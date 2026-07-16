#!/usr/bin/env python3
"""Verify and materialize the manifest-pinned Git Runtime release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "vibe" / "git_runtime_manifest.json"
RELEASE_DOWNLOAD_ROOT = "https://github.com/avibe-bot/avibe/releases/download"
RELEASE_TAG_RE = re.compile(r"git-runtime-v[0-9]+\.[0-9]+\.[0-9]+-[1-9][0-9]*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ReleaseGuardError(RuntimeError):
    """Raised when the pinned release cannot be trusted or materialized."""


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
    manifest_path: Path
    manifest_bytes: bytes
    release_tag: str
    git_version: str
    release_base_url: str
    archives: tuple[ArchiveSpec, ...]

    @property
    def expected_asset_names(self) -> set[str]:
        names = {"git-runtime-manifest.json"}
        for archive in self.archives:
            names.add(archive.name)
            names.add(f"{archive.name}.sha256")
        return names


def _require_string(payload: dict, key: str, *, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ReleaseGuardError(f"{context}.{key} must be a non-empty string")
    return value


def load_release_spec(manifest_path: Path) -> ReleaseSpec:
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseGuardError(f"cannot read Git Runtime manifest: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ReleaseGuardError("Git Runtime manifest schema_version must be 1")
    if payload.get("release_state") != "published":
        raise ReleaseGuardError("Git Runtime manifest must describe a published release")

    release_tag = _require_string(payload, "release_tag", context="manifest")
    if RELEASE_TAG_RE.fullmatch(release_tag) is None:
        raise ReleaseGuardError(f"invalid Git Runtime release tag: {release_tag}")
    git_version = _require_string(payload, "git_version", context="manifest")
    release_base_url = f"{RELEASE_DOWNLOAD_ROOT}/{release_tag}"

    archives_payload = payload.get("archives")
    if not isinstance(archives_payload, dict) or not archives_payload:
        raise ReleaseGuardError("Git Runtime manifest must contain archives")

    archives: list[ArchiveSpec] = []
    seen_names: set[str] = set()
    for platform, raw_archive in sorted(archives_payload.items()):
        context = f"archives.{platform}"
        if not isinstance(platform, str) or not isinstance(raw_archive, dict):
            raise ReleaseGuardError(f"{context} must be an object")
        name = _require_string(raw_archive, "name", context=context)
        url = _require_string(raw_archive, "url", context=context)
        sha256 = _require_string(raw_archive, "sha256", context=context)
        binary_sha256 = _require_string(raw_archive, "binary_sha256", context=context)
        bin_path = _require_string(raw_archive, "bin_path", context=context)
        size = raw_archive.get("size")
        if name in seen_names:
            raise ReleaseGuardError(f"duplicate archive name: {name}")
        if url != f"{release_base_url}/{name}":
            raise ReleaseGuardError(f"{context}.url is outside the pinned release")
        if SHA256_RE.fullmatch(sha256) is None:
            raise ReleaseGuardError(f"{context}.sha256 is invalid")
        if SHA256_RE.fullmatch(binary_sha256) is None:
            raise ReleaseGuardError(f"{context}.binary_sha256 is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ReleaseGuardError(f"{context}.size must be a positive integer")
        if Path(bin_path).is_absolute() or ".." in Path(bin_path).parts:
            raise ReleaseGuardError(f"{context}.bin_path must stay inside the archive")
        seen_names.add(name)
        archives.append(
            ArchiveSpec(
                platform=platform,
                name=name,
                url=url,
                sha256=sha256,
                binary_sha256=binary_sha256,
                size=size,
                bin_path=bin_path,
            )
        )

    return ReleaseSpec(
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        release_tag=release_tag,
        git_version=git_version,
        release_base_url=release_base_url,
        archives=tuple(archives),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(path: Path, archive: ArchiveSpec) -> None:
    if not path.is_file():
        raise ReleaseGuardError(f"missing release archive: {archive.name}")
    if path.stat().st_size != archive.size:
        raise ReleaseGuardError(f"release archive size mismatch: {archive.name}")
    if _sha256_file(path) != archive.sha256:
        raise ReleaseGuardError(f"release archive checksum mismatch: {archive.name}")
    try:
        with tarfile.open(path, "r:gz") as bundle:
            member = bundle.getmember(archive.bin_path)
            binary = bundle.extractfile(member)
            if binary is None or not member.isfile():
                raise ReleaseGuardError(f"release archive is missing {archive.bin_path}: {archive.name}")
            binary_sha256 = hashlib.sha256(binary.read()).hexdigest()
    except (KeyError, tarfile.TarError, OSError) as exc:
        raise ReleaseGuardError(f"invalid release archive {archive.name}: {exc}") from exc
    if binary_sha256 != archive.binary_sha256:
        raise ReleaseGuardError(f"release binary checksum mismatch: {archive.name}")


def _verify_sidecar(path: Path, archive: ArchiveSpec) -> None:
    if not path.is_file():
        raise ReleaseGuardError(f"missing checksum sidecar: {path.name}")
    try:
        fields = path.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise ReleaseGuardError(f"cannot read checksum sidecar {path.name}: {exc}") from exc
    if fields != [archive.sha256, archive.name]:
        raise ReleaseGuardError(f"checksum sidecar mismatch: {path.name}")


def verify_release_assets(manifest_path: Path, asset_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    if not asset_dir.is_dir():
        raise ReleaseGuardError(f"release asset directory is missing: {asset_dir}")
    entries = list(asset_dir.iterdir())
    unsafe_entries = sorted(path.name for path in entries if path.is_symlink() or not path.is_file())
    if unsafe_entries:
        raise ReleaseGuardError(f"release asset directory contains unsafe entries: {unsafe_entries}")
    actual_names = {path.name for path in entries}
    if actual_names != spec.expected_asset_names:
        missing = sorted(spec.expected_asset_names - actual_names)
        unexpected = sorted(actual_names - spec.expected_asset_names)
        raise ReleaseGuardError(f"release asset set mismatch: missing={missing}, unexpected={unexpected}")
    release_manifest = asset_dir / "git-runtime-manifest.json"
    if release_manifest.read_bytes() != spec.manifest_bytes:
        raise ReleaseGuardError("published Git Runtime manifest differs from the packaged manifest")
    for archive in spec.archives:
        _verify_archive(asset_dir / archive.name, archive)
        _verify_sidecar(asset_dir / f"{archive.name}.sha256", archive)
    return spec


def _download_to_path(url: str, destination: Path, *, attempts: int = 3, timeout: float = 60.0) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "avibe-git-runtime-release-guard/1"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            return
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts:
                raise ReleaseGuardError(f"release asset download failed ({exc.code}): {url}") from exc
        except (OSError, urllib.error.URLError) as exc:
            if attempt == attempts:
                raise ReleaseGuardError(f"release asset download failed: {url}: {exc}") from exc
        time.sleep(float(attempt))


def fetch_release_assets(manifest_path: Path, output_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        for archive in spec.archives:
            _download_to_path(archive.url, temporary_dir / archive.name)
            _download_to_path(f"{archive.url}.sha256", temporary_dir / f"{archive.name}.sha256")
        _download_to_path(
            f"{spec.release_base_url}/git-runtime-manifest.json",
            temporary_dir / "git-runtime-manifest.json",
        )
        verify_release_assets(manifest_path, temporary_dir)
        if output_dir.exists():
            if output_dir.is_dir():
                shutil.rmtree(output_dir)
            else:
                output_dir.unlink()
        temporary_dir.replace(output_dir)
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)
    return spec


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch", help="Download and verify the published release assets.")
    fetch.add_argument("--output-dir", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="Verify a previously materialized asset directory.")
    verify.add_argument("--asset-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "fetch":
            spec = fetch_release_assets(args.manifest, args.output_dir)
        else:
            spec = verify_release_assets(args.manifest, args.asset_dir)
    except ReleaseGuardError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "release_tag": spec.release_tag,
                "git_version": spec.git_version,
                "asset_count": len(spec.expected_asset_names),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
