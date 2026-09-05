#!/usr/bin/env python3
"""Verify and materialize the manifest-pinned Model Hub engine release."""

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
DEFAULT_MANIFEST = REPO_ROOT / "vibe" / "model_hub_runtime" / "cliproxyapi_manifest.json"
OWNED_RELEASE_ROOT = "https://github.com/avibe-bot/avibe/releases/download"
UPSTREAM_RELEASE_ROOT = "https://github.com/router-for-me/CLIProxyAPI/releases/download"
EXPECTED_PLATFORMS = frozenset(
    {"darwin-arm64", "darwin-x64", "linux-amd64", "linux-arm64"}
)
UPSTREAM_PLATFORM_NAMES = {
    "darwin-arm64": "darwin_aarch64",
    "darwin-x64": "darwin_amd64",
    "linux-amd64": "linux_amd64",
    "linux-arm64": "linux_aarch64",
}
ASSET_TAG_RE = re.compile(
    r"model-hub-engine-v[0-9]+\.[0-9]+\.[0-9]+-[1-9][0-9]*"
)
VERSION_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


class ReleaseGuardError(RuntimeError):
    """Raised when a pinned CPA release cannot be trusted or materialized."""


@dataclass(frozen=True)
class ArchiveSpec:
    platform: str
    name: str
    owned_url: str
    upstream_url: str
    size: int
    sha256: str
    binary_sha256: str
    bin_path: str


@dataclass(frozen=True)
class ReleaseSpec:
    manifest_bytes: bytes
    version: str
    upstream_release_tag: str
    asset_release_tag: str
    owned_release_url: str
    archives: tuple[ArchiveSpec, ...]

    @property
    def expected_asset_names(self) -> set[str]:
        names = {"model-hub-engine-manifest.json"}
        for archive in self.archives:
            names.add(archive.name)
            names.add(f"{archive.name}.sha256")
        return names


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
        raise ReleaseGuardError(f"cannot read Model Hub engine manifest: {exc}") from exc

    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise ReleaseGuardError("Model Hub engine manifest schema_version must be 1")
    if payload.get("name") != "cliproxyapi":
        raise ReleaseGuardError("Model Hub engine manifest name must be cliproxyapi")
    if payload.get("source") != "router-for-me/CLIProxyAPI":
        raise ReleaseGuardError("Model Hub engine manifest source is invalid")
    if payload.get("license") != "MIT":
        raise ReleaseGuardError("Model Hub engine manifest license is invalid")

    version = _required_string(payload, "version", "manifest")
    upstream_release_tag = _required_string(payload, "release_tag", "manifest")
    asset_release_tag = _required_string(payload, "asset_release_tag", "manifest")
    source_sha = _required_string(payload, "source_sha", "manifest")
    source_url = _required_string(payload, "source_url", "manifest")
    if VERSION_RE.fullmatch(version) is None or upstream_release_tag != version:
        raise ReleaseGuardError("Model Hub engine version and upstream release tag are invalid")
    if ASSET_TAG_RE.fullmatch(asset_release_tag) is None:
        raise ReleaseGuardError("Model Hub engine asset release tag is invalid")
    if not asset_release_tag.startswith(f"model-hub-engine-{version}-"):
        raise ReleaseGuardError("Model Hub engine asset release tag does not match version")
    if SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise ReleaseGuardError("Model Hub engine source SHA is invalid")
    if source_url != f"https://github.com/router-for-me/CLIProxyAPI/tree/{source_sha}":
        raise ReleaseGuardError("Model Hub engine source URL is invalid")

    owned_release_url = f"{OWNED_RELEASE_ROOT}/{asset_release_tag}"
    upstream_release_url = f"{UPSTREAM_RELEASE_ROOT}/{upstream_release_tag}"
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ReleaseGuardError("Model Hub engine manifest assets must be a list")

    archives: list[ArchiveSpec] = []
    seen_platforms: set[str] = set()
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_assets):
        context = f"assets[{index}]"
        if not isinstance(raw, dict):
            raise ReleaseGuardError(f"{context} must be an object")
        platform = _required_string(raw, "platform", context)
        owned_url = _required_string(raw, "url", context)
        sha256 = _required_string(raw, "sha256", context)
        binary_sha256 = _required_string(raw, "binary_sha256", context)
        bin_path = _required_string(raw, "bin_path", context)
        size = raw.get("size_bytes")
        name = owned_url.rsplit("/", 1)[-1]
        expected_name = (
            f"CLIProxyAPI_{version.removeprefix('v')}_"
            f"{UPSTREAM_PLATFORM_NAMES.get(platform, '')}.tar.gz"
        )
        if platform in seen_platforms or platform not in EXPECTED_PLATFORMS:
            raise ReleaseGuardError(f"{context}.platform is invalid or duplicated")
        if Path(name).name != name or name in seen_names or name != expected_name:
            raise ReleaseGuardError(f"{context}.url has an invalid asset name")
        if owned_url != f"{owned_release_url}/{name}":
            raise ReleaseGuardError(f"{context}.url is outside the Avibe-owned release")
        if SHA256_RE.fullmatch(sha256) is None or SHA256_RE.fullmatch(binary_sha256) is None:
            raise ReleaseGuardError(f"{context} has an invalid digest")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ReleaseGuardError(f"{context}.size_bytes is invalid")
        if bin_path != "cli-proxy-api":
            raise ReleaseGuardError(f"{context}.bin_path is invalid")
        seen_platforms.add(platform)
        seen_names.add(name)
        archives.append(
            ArchiveSpec(
                platform=platform,
                name=name,
                owned_url=owned_url,
                upstream_url=f"{upstream_release_url}/{name}",
                size=size,
                sha256=sha256,
                binary_sha256=binary_sha256,
                bin_path=bin_path,
            )
        )
    if seen_platforms != EXPECTED_PLATFORMS:
        raise ReleaseGuardError("Model Hub engine manifest platform set is invalid")

    return ReleaseSpec(
        manifest_bytes=manifest_bytes,
        version=version,
        upstream_release_tag=upstream_release_tag,
        asset_release_tag=asset_release_tag,
        owned_release_url=owned_release_url,
        archives=tuple(sorted(archives, key=lambda archive: archive.platform)),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(path: Path, archive: ArchiveSpec) -> None:
    if not path.is_file() or path.stat().st_size != archive.size:
        raise ReleaseGuardError(f"Model Hub engine archive size mismatch: {archive.name}")
    if _sha256(path) != archive.sha256:
        raise ReleaseGuardError(f"Model Hub engine archive checksum mismatch: {archive.name}")
    try:
        with tarfile.open(path, "r:gz") as bundle:
            member = bundle.getmember(archive.bin_path)
            stream = bundle.extractfile(member)
            binary_sha256 = (
                hashlib.sha256(stream.read()).hexdigest()
                if stream is not None and member.isfile()
                else ""
            )
    except (KeyError, OSError, tarfile.TarError) as exc:
        raise ReleaseGuardError(f"invalid Model Hub engine archive: {archive.name}") from exc
    if binary_sha256 != archive.binary_sha256:
        raise ReleaseGuardError(f"Model Hub engine binary checksum mismatch: {archive.name}")


def _verify_sidecar(path: Path, archive: ArchiveSpec) -> None:
    if not path.is_file():
        raise ReleaseGuardError(f"missing Model Hub engine checksum: {path.name}")
    try:
        fields = path.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise ReleaseGuardError(f"cannot read Model Hub engine checksum: {path.name}") from exc
    if fields != [archive.sha256, archive.name]:
        raise ReleaseGuardError(f"Model Hub engine checksum sidecar mismatch: {path.name}")


def verify_release_assets(manifest_path: Path, asset_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    if not asset_dir.is_dir():
        raise ReleaseGuardError("Model Hub engine asset directory is missing")
    entries = list(asset_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ReleaseGuardError("Model Hub engine asset directory contains unsafe entries")
    actual_names = {path.name for path in entries}
    if actual_names != spec.expected_asset_names:
        raise ReleaseGuardError(
            "Model Hub engine asset set mismatch: "
            f"missing={sorted(spec.expected_asset_names - actual_names)}, "
            f"unexpected={sorted(actual_names - spec.expected_asset_names)}"
        )
    if (asset_dir / "model-hub-engine-manifest.json").read_bytes() != spec.manifest_bytes:
        raise ReleaseGuardError("published Model Hub engine manifest differs from the packaged manifest")
    for archive in spec.archives:
        _verify_archive(asset_dir / archive.name, archive)
        _verify_sidecar(asset_dir / f"{archive.name}.sha256", archive)
    return spec


def _download(url: str, destination: Path, *, max_bytes: int, attempts: int = 3) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "avibe-model-hub-engine-release-guard/1"},
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise ReleaseGuardError(f"release asset exceeds its size limit: {url}")
                    output.write(chunk)
            return
        except ReleaseGuardError:
            destination.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            destination.unlink(missing_ok=True)
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == attempts:
                raise ReleaseGuardError(f"release asset download failed ({exc.code}): {url}") from exc
        except (OSError, urllib.error.URLError) as exc:
            destination.unlink(missing_ok=True)
            if attempt == attempts:
                raise ReleaseGuardError(f"release asset download failed: {url}: {exc}") from exc
        time.sleep(float(attempt))


def _materialize(
    manifest_path: Path,
    output_dir: Path,
    *,
    from_upstream: bool,
) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        (staging / "model-hub-engine-manifest.json").write_bytes(spec.manifest_bytes)
        for archive in spec.archives:
            url = archive.upstream_url if from_upstream else archive.owned_url
            _download(url, staging / archive.name, max_bytes=archive.size)
            if (staging / archive.name).stat().st_size != archive.size:
                raise ReleaseGuardError(f"Model Hub engine archive size mismatch: {archive.name}")
            if from_upstream:
                (staging / f"{archive.name}.sha256").write_text(
                    f"{archive.sha256}  {archive.name}\n",
                    encoding="utf-8",
                )
            else:
                _download(
                    f"{archive.owned_url}.sha256",
                    staging / f"{archive.name}.sha256",
                    max_bytes=256,
                )
        if not from_upstream:
            _download(
                f"{spec.owned_release_url}/model-hub-engine-manifest.json",
                staging / "model-hub-engine-manifest.json",
                max_bytes=max(len(spec.manifest_bytes), 1),
            )
        verify_release_assets(manifest_path, staging)
        if output_dir.exists():
            if not output_dir.is_dir() or output_dir.is_symlink():
                raise ReleaseGuardError("Model Hub engine output path is not a safe directory")
            shutil.rmtree(output_dir)
        staging.replace(output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return spec


def fetch_release_assets(manifest_path: Path, output_dir: Path) -> ReleaseSpec:
    return _materialize(manifest_path, output_dir, from_upstream=False)


def fetch_upstream_assets(manifest_path: Path, output_dir: Path) -> ReleaseSpec:
    return _materialize(manifest_path, output_dir, from_upstream=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch", help="Fetch and verify Avibe-owned release assets.")
    fetch.add_argument("--output-dir", type=Path, required=True)
    fetch_source = subparsers.add_parser(
        "fetch-source",
        help="Fetch verified upstream bytes for initial Avibe release publication.",
    )
    fetch_source.add_argument("--output-dir", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="Verify a materialized release directory.")
    verify.add_argument("--asset-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "fetch":
            spec = fetch_release_assets(args.manifest, args.output_dir)
        elif args.command == "fetch-source":
            spec = fetch_upstream_assets(args.manifest, args.output_dir)
        else:
            spec = verify_release_assets(args.manifest, args.asset_dir)
    except ReleaseGuardError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "version": spec.version,
                "asset_release_tag": spec.asset_release_tag,
                "asset_count": len(spec.expected_asset_names),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
