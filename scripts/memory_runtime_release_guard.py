#!/usr/bin/env python3
"""Verify and materialize manifest-pinned Memory Runtime release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

try:
    from scripts.release_package_version import package_version_from_release_tag
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repository root.
    from release_package_version import package_version_from_release_tag

RELEASE_DOWNLOAD_ROOT = "https://github.com/avibe-bot/avibe/releases/download"
EXPECTED_EVEROS_VERSION = "1.2.3"
EXPECTED_PYTHON_VERSION = "3.12.12"
EXPECTED_LOCK_SHA256 = "e6acc17e4c0969563d380326e90134965af0822259bb4a9adb4d54433e9737fe"
EXPECTED_UV_VERSION = "0.9.18"
EXPECTED_PLATFORMS = frozenset({"darwin-arm64", "linux-arm64", "linux-x64"})
EXPECTED_SYNC_BOOTSTRAP_REVISION = 1
EXPECTED_SYNC_ARGV = ["-I", "-m", "everos.entrypoints.cli.main", "cascade", "sync"]
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
ASSET_FAILURE_EXIT = 1
POLICY_EXCLUSION_EXIT = 2
INTERNAL_GUARD_FAILURE_EXIT = 3
PACKAGE_POLICY_SCHEMA_VERSION = 1
SUPPORTED_NAMESPACE_POLICY_VERSIONS = frozenset({1})


class ReleaseGuardError(RuntimeError):
    """Base error for Memory Runtime release guard failures."""


class ManifestPolicyError(ReleaseGuardError):
    """Raised when a manifest is outside the guard's verifiable policy scope."""


class ReleaseAssetError(ReleaseGuardError):
    """Raised when guarded release bytes are unavailable or fail verification."""


@dataclass(frozen=True)
class RuntimeProvenance:
    python_version: str
    lock_sha256: str
    uv_version: str


# Existing published manifests remain verifiable after the current pin moves.
PUBLISHED_RUNTIME_PROVENANCE = {
    "1.1.3": RuntimeProvenance(
        python_version=EXPECTED_PYTHON_VERSION,
        lock_sha256="62b00f1a9ca04cc4ea4c5af51f389ba49acdea8786e5f7044d52823244502c57",
        uv_version=EXPECTED_UV_VERSION,
    ),
    "1.2.1": RuntimeProvenance(
        python_version=EXPECTED_PYTHON_VERSION,
        lock_sha256="e7b59ee874e5cb2bfcbcb87cbd1e9c2d6ca2df752cd8a1059ddd3badb8c0246f",
        uv_version=EXPECTED_UV_VERSION,
    ),
    EXPECTED_EVEROS_VERSION: RuntimeProvenance(
        python_version=EXPECTED_PYTHON_VERSION,
        lock_sha256=EXPECTED_LOCK_SHA256,
        uv_version=EXPECTED_UV_VERSION,
    ),
}


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
    sync_bootstrap_sha256: str | None = None
    sync_scrubbers_sha256: str | None = None

    @property
    def expected_asset_names(self) -> set[str]:
        return {"memory-runtime-manifest.json", *(archive.name for archive in self.archives)}


@dataclass(frozen=True)
class PackageMetadata:
    name: str
    version: Version
    requires_python: str
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class PackageReleasePolicy:
    requires_python: str
    supported_python_versions: tuple[Version, ...]
    namespace_policy_version: int


@dataclass(frozen=True)
class RequirementClassification:
    requirement: Requirement
    exact_version: Version | None


@dataclass(frozen=True)
class StaticTransitionVerification:
    core: PackageMetadata
    memory: PackageMetadata
    policy: PackageReleasePolicy


def _release_version(release_tag: str) -> Version:
    try:
        return Version(package_version_from_release_tag(release_tag))
    except (ValueError, InvalidVersion) as exc:
        raise ReleaseAssetError(f"invalid release tag: {release_tag!r}") from exc


def load_package_release_policy(
    manifest_bytes: bytes,
    *,
    expected_manifest: bytes,
    release_tag: str,
) -> PackageReleasePolicy:
    """Load the B-facing package policy only after exact manifest identity."""
    if manifest_bytes != expected_manifest:
        raise ReleaseAssetError("transition package manifest does not match the selected manifest")
    try:
        payload = json.loads(manifest_bytes)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestPolicyError("Memory artifact package policy manifest is invalid") from exc
    if type(payload) is not dict or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ManifestPolicyError("Memory Runtime manifest schema_version must be integer 1")
    raw = payload.get("package_policy")
    keys = {
        "schema_version",
        "release_tag",
        "release_family",
        "requires_python",
        "supported_python_versions",
        "namespace_policy_version",
    }
    if type(raw) is not dict or set(raw) != keys:
        raise ManifestPolicyError("manifest package_policy schema is invalid")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != PACKAGE_POLICY_SCHEMA_VERSION:
        raise ManifestPolicyError("manifest package_policy schema_version must be integer 1")
    if any(type(raw[key]) is not str or not raw[key] for key in ("release_tag", "release_family", "requires_python")):
        raise ManifestPolicyError("manifest package_policy string fields are invalid")
    versions = raw["supported_python_versions"]
    namespace_version = raw["namespace_policy_version"]
    if (
        type(versions) is not list
        or not versions
        or any(type(version) is not str or not version for version in versions)
        or len(versions) != len(set(versions))
        or type(namespace_version) is not int
        or namespace_version not in SUPPORTED_NAMESPACE_POLICY_VERSIONS
    ):
        raise ManifestPolicyError("manifest package_policy typed fields are invalid")
    release_version = _release_version(release_tag)
    if raw["release_tag"] != release_tag or raw["release_family"] != f"{release_version.major}.{release_version.minor}":
        raise ManifestPolicyError("manifest package_policy release identity is invalid")
    try:
        requires_python = SpecifierSet(raw["requires_python"])
        supported = tuple(Version(version) for version in versions)
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise ManifestPolicyError("manifest package_policy Python contract is invalid") from exc
    if len(supported) != len(set(supported)) or any(version not in requires_python for version in supported):
        raise ManifestPolicyError("manifest package_policy excludes a supported Python version")
    return PackageReleasePolicy(raw["requires_python"], supported, namespace_version)


def classify_requirement(raw: str) -> RequirementClassification:
    """Classify one valid requirement without treating wildcard equality as exact."""
    try:
        requirement = Requirement(raw)
    except InvalidRequirement as exc:
        raise ReleaseAssetError(f"invalid Requires-Dist requirement: {raw!r}") from exc
    specifiers = tuple(requirement.specifier)
    exact_version = None
    if (
        not requirement.extras
        and requirement.url is None
        and requirement.marker is None
        and len(specifiers) == 1
        and specifiers[0].operator == "=="
        and "*" not in specifiers[0].version
    ):
        try:
            exact_version = Version(specifiers[0].version)
        except InvalidVersion as exc:
            raise ReleaseAssetError(f"invalid exact requirement version: {raw!r}") from exc
    return RequirementClassification(requirement, exact_version)


def inspect_wheel(wheel: Path, *, python_executable: Path = Path(sys.executable)) -> PackageMetadata:
    """Validate and inspect a wheel through packaging and pip's no-install report."""
    try:
        filename_name, filename_version = parse_wheel_filename(wheel.name)[:2]
    except InvalidWheelFilename as exc:
        raise ReleaseAssetError(f"invalid wheel filename: {wheel.name}") from exc
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR="1", PYTHONPATH="")
    command = [
        str(python_executable), "-m", "pip", "--isolated", "install", "--dry-run",
        "--ignore-installed", "--ignore-requires-python", "--no-deps", "--no-index",
        "--no-cache-dir", "--disable-pip-version-check", "--report", "-", "--quiet",
        str(wheel.resolve()),
    ]
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ReleaseAssetError(f"wheel structure validation could not run for {wheel.name}: {exc}") from exc
    if result.returncode:
        raise ReleaseAssetError(f"wheel structure validation failed for {wheel.name}: {result.stderr}")
    try:
        report = json.loads(result.stdout)
        installs = report["install"]
        if type(installs) is not list or len(installs) != 1 or type(installs[0]) is not dict:
            raise ValueError
        metadata = installs[0]["metadata"]
        if type(metadata) is not dict:
            raise ValueError
        raw_requires_dist = metadata.get("requires_dist", [])
        if any(type(metadata.get(key)) is not str or not metadata[key] for key in ("name", "version", "requires_python")):
            raise ValueError
        if type(raw_requires_dist) is not list or any(type(item) is not str for item in raw_requires_dist):
            raise ValueError
        parsed_version = Version(metadata["version"])
    except (KeyError, IndexError, TypeError, ValueError, InvalidVersion, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"pip returned invalid wheel metadata for {wheel.name}") from exc
    parsed = PackageMetadata(
        str(canonicalize_name(metadata["name"])),
        parsed_version,
        metadata["requires_python"],
        tuple(sorted(raw_requires_dist)),
    )
    if parsed.name != str(filename_name) or parsed.version != filename_version:
        raise ReleaseAssetError(f"wheel filename identity differs from metadata: {wheel.name}")
    return parsed


def _requirements_for(metadata: PackageMetadata, name: str) -> tuple[RequirementClassification, ...]:
    expected = canonicalize_name(name)
    classified = tuple(classify_requirement(raw) for raw in metadata.requires_dist)
    return tuple(item for item in classified if canonicalize_name(item.requirement.name) == expected)


def verify_static_transition(
    core_wheel: Path,
    memory_wheel: Path,
    *,
    release_tag: str,
    manifest_bytes: bytes,
    expected_manifest: bytes,
    python_executable: Path = Path(sys.executable),
) -> StaticTransitionVerification:
    """Freeze A's static contract for successor B's staged and rebuilt wheels."""
    policy = load_package_release_policy(
        manifest_bytes, expected_manifest=expected_manifest, release_tag=release_tag
    )
    core = inspect_wheel(core_wheel, python_executable=python_executable)
    memory = inspect_wheel(memory_wheel, python_executable=python_executable)
    expected_version = _release_version(release_tag)
    if core.name != "avibe-os" or memory.name != "avibe-memory":
        raise ReleaseAssetError("transition wheel distribution identity is invalid")
    if core.version != expected_version or memory.version != expected_version:
        raise ReleaseAssetError("wheel metadata version does not match the release tag")
    if core.requires_python != policy.requires_python or memory.requires_python != policy.requires_python:
        raise ReleaseAssetError(f"wheel Requires-Python must match release policy {policy.requires_python}")
    memory_dependencies = _requirements_for(core, "avibe-memory")
    if len(memory_dependencies) != 1 or memory_dependencies[0].exact_version != expected_version:
        raise ReleaseAssetError("transition core must hard-depend on the exact Memory version")
    core_dependencies = _requirements_for(memory, "avibe-os")
    if len(core_dependencies) != 1:
        raise ReleaseAssetError("Memory metadata must contain exactly one avibe-os dependency")
    reverse = core_dependencies[0].requirement
    if reverse.url is not None or reverse.marker is not None or reverse.extras or not reverse.specifier or expected_version not in reverse.specifier:
        raise ReleaseAssetError("Memory avibe-os dependency must accept the release version")
    return StaticTransitionVerification(core, memory, policy)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(payload: dict, key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestPolicyError(f"{context}.{key} must be a non-empty string")
    return value


def load_release_spec(manifest_path: Path) -> ReleaseSpec:
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestPolicyError(f"cannot read Memory Runtime manifest: {exc}") from exc
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ManifestPolicyError("Memory Runtime manifest schema_version must be 1")
    everos_version = payload.get("everos_version")
    provenance = (
        PUBLISHED_RUNTIME_PROVENANCE.get(everos_version)
        if isinstance(everos_version, str)
        else None
    )
    if payload.get("release_state") != "published" or provenance is None:
        raise ManifestPolicyError("Memory Runtime manifest must describe a published supported EverOS version")
    if (
        payload.get("python_version") != provenance.python_version
        or payload.get("lock_sha256") != provenance.lock_sha256
        or payload.get("lock_id") != f"uv-lock-sha256:{provenance.lock_sha256}"
        or payload.get("uv_version") != provenance.uv_version
    ):
        raise ManifestPolicyError("Memory Runtime manifest provenance is invalid")
    sync_revision = payload.get("sync_bootstrap_revision")
    sync_argv = payload.get("sync_argv")
    sync_digest = payload.get("sync_bootstrap_sha256")
    sync_scrubbers_digest = payload.get("sync_scrubbers_sha256")
    if (
        sync_revision is not None
        or sync_argv is not None
        or sync_digest is not None
        or sync_scrubbers_digest is not None
    ):
        if (
            sync_revision != EXPECTED_SYNC_BOOTSTRAP_REVISION
            or sync_argv != EXPECTED_SYNC_ARGV
            or not isinstance(sync_digest, str)
            or len(sync_digest) != 64
            or any(character not in "0123456789abcdef" for character in sync_digest)
            or not isinstance(sync_scrubbers_digest, str)
            or len(sync_scrubbers_digest) != 64
            or any(character not in "0123456789abcdef" for character in sync_scrubbers_digest)
        ):
            raise ManifestPolicyError("Memory Runtime sync bootstrap contract is invalid")

    release_tag = _required_string(payload, "release_tag", "manifest")
    release_root = f"{RELEASE_DOWNLOAD_ROOT}/{release_tag}"
    raw_archives = payload.get("archives")
    if not isinstance(raw_archives, dict) or set(raw_archives) != EXPECTED_PLATFORMS:
        raise ManifestPolicyError("Memory Runtime manifest platform set is invalid")
    archives: list[ArchiveSpec] = []
    for platform, raw in sorted(raw_archives.items()):
        if not isinstance(raw, dict):
            raise ManifestPolicyError(f"archives.{platform} must be an object")
        context = f"archives.{platform}"
        name = _required_string(raw, "name", context)
        url = _required_string(raw, "url", context)
        sha256 = _required_string(raw, "sha256", context)
        binary_sha256 = _required_string(raw, "binary_sha256", context)
        bin_path = _required_string(raw, "bin_path", context)
        size = raw.get("size")
        if Path(name).name != name or url != f"{release_root}/{name}":
            raise ManifestPolicyError(f"{context} is outside the pinned release")
        if (
            len(sha256) != 64
            or len(binary_sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256 + binary_sha256)
        ):
            raise ManifestPolicyError(f"{context} has an invalid digest")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ManifestPolicyError(f"{context}.size is invalid")
        if Path(bin_path).is_absolute() or ".." in Path(bin_path).parts:
            raise ManifestPolicyError(f"{context}.bin_path is unsafe")
        archives.append(ArchiveSpec(platform, name, url, sha256, binary_sha256, size, bin_path))
    return ReleaseSpec(
        manifest_bytes=manifest_bytes,
        release_tag=release_tag,
        archives=tuple(archives),
        sync_bootstrap_sha256=sync_digest,
        sync_scrubbers_sha256=sync_scrubbers_digest,
    )


def verify_release_assets(manifest_path: Path, asset_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    if not asset_dir.is_dir():
        raise ReleaseAssetError("Memory Runtime asset directory is missing")
    entries = list(asset_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ReleaseAssetError("Memory Runtime asset directory contains unsafe entries")
    actual = {path.name for path in entries}
    if actual != spec.expected_asset_names:
        raise ReleaseAssetError(
            f"Memory Runtime asset set mismatch: missing={sorted(spec.expected_asset_names - actual)}, "
            f"unexpected={sorted(actual - spec.expected_asset_names)}"
        )
    if (asset_dir / "memory-runtime-manifest.json").read_bytes() != spec.manifest_bytes:
        raise ReleaseAssetError("published Memory Runtime manifest differs from the pinned manifest")
    for archive in spec.archives:
        path = asset_dir / archive.name
        if path.stat().st_size != archive.size or _sha256(path) != archive.sha256:
            raise ReleaseAssetError(f"Memory Runtime archive integrity mismatch: {archive.name}")
        try:
            with tarfile.open(path, "r:gz") as bundle:
                member = bundle.getmember(archive.bin_path)
                binary = bundle.extractfile(member)
                digest = hashlib.sha256(binary.read()).hexdigest() if binary is not None and member.isfile() else ""
                if spec.sync_bootstrap_sha256 is not None:
                    bootstrap_digest = _archive_member_sha256(
                        bundle,
                        "lib/python3.12/site-packages/avibe_memory_sync_bootstrap.py",
                    )
                    scrubbers_digest = _archive_member_sha256(
                        bundle,
                        "lib/python3.12/site-packages/avibe_memory_sync_scrubbers.py",
                    )
                    marker = bundle.extractfile(
                        bundle.getmember(
                            "lib/python3.12/site-packages/avibe_memory_sync_bootstrap.pth"
                        )
                    )
                    if (
                        bootstrap_digest != spec.sync_bootstrap_sha256
                        or scrubbers_digest != spec.sync_scrubbers_sha256
                        or marker is None
                        or marker.read() != b"import avibe_memory_sync_bootstrap\n"
                    ):
                        raise ReleaseAssetError(
                            f"Memory Runtime sync contract mismatch: {archive.name}"
                        )
        except (KeyError, OSError, tarfile.TarError) as exc:
            raise ReleaseAssetError(f"invalid Memory Runtime archive: {archive.name}") from exc
        if digest != archive.binary_sha256:
            raise ReleaseAssetError(f"Memory Runtime binary integrity mismatch: {archive.name}")
    return spec


def _archive_member_sha256(bundle: tarfile.TarFile, name: str) -> str:
    member = bundle.getmember(name)
    stream = bundle.extractfile(member)
    if stream is None or not member.isfile():
        return ""
    return hashlib.sha256(stream.read()).hexdigest()


def _download(url: str, destination: Path, expected_size: int, attempts: int = 3) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "avibe-memory-runtime-release-guard/1"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
                content_length = response.headers.get("Content-Length")
                try:
                    declared_size = int(content_length) if content_length is not None else None
                except (TypeError, ValueError):
                    declared_size = None
                if declared_size is not None and declared_size > expected_size:
                    raise ReleaseAssetError(f"release asset exceeds manifest size: {url}")
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > expected_size:
                        raise ReleaseAssetError(f"release asset exceeds manifest size: {url}")
                    output.write(chunk)
            return
        except ReleaseAssetError:
            destination.unlink(missing_ok=True)
            raise
        except (OSError, urllib.error.URLError) as exc:
            destination.unlink(missing_ok=True)
            if attempt == attempts:
                raise ReleaseAssetError(f"release asset download failed: {url}: {exc}") from exc
            time.sleep(float(attempt))


def fetch_release_assets(manifest_path: Path, output_dir: Path) -> ReleaseSpec:
    spec = load_release_spec(manifest_path)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        manifest_url = f"{RELEASE_DOWNLOAD_ROOT}/{spec.release_tag}/memory-runtime-manifest.json"
        _download(
            manifest_url,
            temporary / "memory-runtime-manifest.json",
            len(spec.manifest_bytes),
        )
        for archive in spec.archives:
            _download(archive.url, temporary / archive.name, archive.size)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch")
    fetch.add_argument("--output-dir", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--asset-dir", type=Path, required=True)
    subparsers.add_parser("check-policy")
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            spec = fetch_release_assets(args.manifest, args.output_dir)
        elif args.command == "verify":
            spec = verify_release_assets(args.manifest, args.asset_dir)
        else:
            spec = load_release_spec(args.manifest)
    except ManifestPolicyError as exc:
        print(
            json.dumps({"ok": False, "failure_kind": "policy", "error": str(exc)}),
            file=sys.stderr,
        )
        return POLICY_EXCLUSION_EXIT
    except ReleaseAssetError as exc:
        print(
            json.dumps({"ok": False, "failure_kind": "bytes", "error": str(exc)}),
            file=sys.stderr,
        )
        return ASSET_FAILURE_EXIT
    except ReleaseGuardError as exc:
        print(
            json.dumps({"ok": False, "failure_kind": "internal", "error": str(exc)}),
            file=sys.stderr,
        )
        return INTERNAL_GUARD_FAILURE_EXIT
    print(json.dumps({"ok": True, "release_tag": spec.release_tag, "asset_count": len(spec.expected_asset_names)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
