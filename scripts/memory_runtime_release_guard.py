#!/usr/bin/env python3
"""Verify and materialize manifest-pinned Memory Runtime release assets."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

try:
    from scripts.release_package_version import package_version_from_release_tag
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repository root.
    from release_package_version import package_version_from_release_tag

RELEASE_DOWNLOAD_ROOT = "https://github.com/avibe-bot/avibe/releases/download"
LAST_LEGACY_RELEASE_VERSION = Version("3.0.14rc2")
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
LEGACY_MANIFEST_ABSENT_EXIT = 4
MEMORY_MANIFEST_PATH = "vibe/memory_runtime_manifest.json"
REQUIRED_MEMORY_IMPLEMENTATION = frozenset({"avibe_memory/__init__.py", "avibe_memory/runtime.py"})


class ReleaseGuardError(RuntimeError):
    """Base error for Memory Runtime release guard failures."""


class ManifestPolicyError(ReleaseGuardError):
    """Raised when a manifest is outside the guard's verifiable policy scope."""


class ReleaseAssetError(ReleaseGuardError):
    """Raised when guarded release bytes are unavailable or fail verification."""


class LegacyManifestAbsent(ReleaseGuardError):
    """Raised when a pre-transition release predates the managed runtime manifest."""


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
    version: str
    requires_python: str
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class PackageArchive:
    path: Path
    metadata: PackageMetadata
    files: dict[str, bytes]


@dataclass(frozen=True)
class PackageReleasePolicy:
    requires_python: str
    supported_python_versions: tuple[str, ...]
    namespace_policy_version: int


@dataclass(frozen=True)
class ManifestDiscovery:
    owner: str
    manifest_bytes: bytes


SdistBuilder = Callable[[Path, Path], Path]

NAMESPACE_POLICIES = {
    1: {
        "avibe-os": frozenset({"config", "core", "modules", "storage", "vibe"}),
        "avibe-memory": frozenset({"avibe_memory"}),
    }
}
PIP_FLAGS = (
    "install",
    "--dry-run",
    "--ignore-installed",
    "--disable-pip-version-check",
    "--no-input",
    "--no-index",
    "--only-binary=:all:",
)


def _run_pip(arguments: list[str], failure: str) -> None:
    pip = shutil.which("pip") or shutil.which("pip3")
    if pip is None:
        raise ReleaseAssetError("offline pip tooling is unavailable")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR="1", PYTHONPATH="")
    result = subprocess.run(
        [pip, *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ReleaseAssetError(f"{failure}: {result.stdout}{result.stderr}")


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


def _filename_identity(path: Path) -> tuple[str, Version]:
    try:
        parsed = parse_wheel_filename(path.name)[:2] if path.name.endswith(".whl") else parse_sdist_filename(path.name)
    except (InvalidWheelFilename, InvalidSdistFilename) as exc:
        raise ReleaseAssetError(f"invalid distribution filename: {path.name}") from exc
    return str(parsed[0]), parsed[1]


def _assert_filename_identity(path: Path, metadata: PackageMetadata) -> None:
    filename_name, filename_version = _filename_identity(path)
    try:
        metadata_version = Version(metadata.version)
    except InvalidVersion as exc:
        raise ReleaseAssetError(f"invalid distribution metadata Version: {path.name}") from exc
    if filename_name != metadata.name or filename_version != metadata_version:
        raise ReleaseAssetError(f"distribution filename identity differs from metadata: {path.name}")


def _metadata(message_bytes: bytes, context: str) -> PackageMetadata:
    try:
        message = Parser().parsestr(message_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ReleaseAssetError(f"{context} metadata is not UTF-8") from exc
    required: dict[str, str] = {}
    for key in ("Name", "Version", "Requires-Python"):
        values = message.get_all(key) or []
        if len(values) != 1 or not values[0]:
            raise ReleaseAssetError(f"{context} metadata must contain exactly one {key}")
        required[key] = values[0]
    return PackageMetadata(
        name=str(canonicalize_name(required["Name"])),
        version=required["Version"],
        requires_python=required["Requires-Python"],
        requires_dist=tuple(sorted(message.get_all("Requires-Dist") or [])),
    )


def _validate_wheel_structure(path: Path) -> None:
    _run_pip(
        [*PIP_FLAGS, "--no-deps", "--ignore-requires-python", str(path.resolve())],
        f"wheel structure validation failed for {path.name}",
    )


def _wheel_archive(path: Path) -> PackageArchive:
    _validate_wheel_structure(path)
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ReleaseAssetError(f"wheel contains duplicate paths: {path.name}")
            if any(
                "\\" in name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts for name in names
            ):
                raise ReleaseAssetError(f"wheel contains an unsafe path: {path.name}")
            files = {name: archive.read(name) for name in names if not name.endswith("/")}
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseAssetError(f"cannot read wheel {path.name}: {exc}") from exc
    metadata_paths = [name for name in files if name.endswith(".dist-info/METADATA")]
    if len(metadata_paths) != 1:
        raise ReleaseAssetError(f"wheel must contain exactly one METADATA file: {path.name}")
    package = PackageArchive(path, _metadata(files[metadata_paths[0]], path.name), files)
    _assert_filename_identity(path, package.metadata)
    return package


def _sdist_archive(path: Path) -> PackageArchive:
    files: dict[str, bytes] = {}
    roots: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                member_path = PurePosixPath(member.name)
                if member_path.is_absolute() or ".." in member_path.parts or not member_path.parts:
                    raise ReleaseAssetError(f"sdist contains an unsafe path: {path.name}")
                roots.add(member_path.parts[0])
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ReleaseAssetError(f"sdist contains a non-regular entry: {path.name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ReleaseAssetError(f"sdist member is unreadable: {member.name}")
                relative = PurePosixPath(*member_path.parts[1:]).as_posix()
                if not relative or relative in files:
                    raise ReleaseAssetError(f"sdist contains duplicate or root-level content: {path.name}")
                files[relative] = stream.read()
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseAssetError(f"cannot read sdist {path.name}: {exc}") from exc
    if len(roots) != 1 or "PKG-INFO" not in files:
        raise ReleaseAssetError(f"sdist must have one root and one PKG-INFO: {path.name}")
    package = PackageArchive(path, _metadata(files["PKG-INFO"], path.name), files)
    _assert_filename_identity(path, package.metadata)
    return package


def _installed_files(package: PackageArchive) -> dict[str, bytes]:
    if package.path.suffix != ".whl":
        return package.files
    installed: dict[str, bytes] = {}
    for name, content in package.files.items():
        parts = PurePosixPath(name).parts
        if len(parts) >= 3 and parts[0].endswith(".data") and parts[1] in {"purelib", "platlib"}:
            name = PurePosixPath(*parts[2:]).as_posix()
        if name in installed:
            raise ReleaseAssetError(f"wheel has colliding install paths: {package.path.name}")
        installed[name] = content
    return installed


def _requirements_for(metadata: PackageMetadata, name: str) -> tuple[Requirement, ...]:
    expected = canonicalize_name(name)
    try:
        requirements = tuple(Requirement(raw) for raw in metadata.requires_dist)
    except InvalidRequirement as exc:
        raise ReleaseAssetError(f"{metadata.name} has invalid Requires-Dist metadata") from exc
    return tuple(item for item in requirements if canonicalize_name(item.name) == expected)


def _exact_memory_pin(metadata: PackageMetadata) -> str | None:
    requirements = _requirements_for(metadata, "avibe-memory")
    if len(requirements) != 1:
        return None
    requirement = requirements[0]
    specifiers = tuple(requirement.specifier)
    if (
        requirement.extras
        or requirement.url is not None
        or requirement.marker is not None
        or len(specifiers) != 1
        or specifiers[0].operator != "=="
    ):
        return None
    return str(Version(specifiers[0].version))


def _package_files(asset_dir: Path, distribution: str, suffix: str) -> list[Path]:
    expected = canonicalize_name(distribution)
    candidates = [path for path in asset_dir.iterdir() if path.name.endswith(suffix)]
    matches = [path for path in candidates if _filename_identity(path)[0] == expected]
    if any(path.is_symlink() or not path.is_file() for path in matches):
        raise ReleaseAssetError(f"{distribution} release artifacts contain an unsafe entry")
    return sorted(matches)


def _release_version(release_tag: str) -> Version:
    try:
        return Version(package_version_from_release_tag(release_tag))
    except (ValueError, InvalidVersion) as exc:
        raise ReleaseAssetError(f"invalid release tag: {release_tag!r}") from exc


def _package_release_policy(payload: dict, release_tag: str) -> PackageReleasePolicy | None:
    raw = payload.get("package_policy")
    if raw is None:
        return None
    keys = {
        "schema_version",
        "release_tag",
        "release_family",
        "requires_python",
        "supported_python_versions",
        "namespace_policy_version",
    }
    if not isinstance(raw, dict) or set(raw) != keys or raw.get("schema_version") != 1:
        raise ManifestPolicyError("manifest package_policy schema is invalid")
    release_version = _release_version(release_tag)
    release_family = f"{release_version.major}.{release_version.minor}"
    requires_python = raw.get("requires_python")
    versions = raw.get("supported_python_versions")
    namespace_policy_version = raw.get("namespace_policy_version")
    if (
        raw.get("release_tag") != release_tag
        or raw.get("release_family") != release_family
        or not isinstance(requires_python, str)
        or not isinstance(versions, list)
        or not versions
        or any(not isinstance(version, str) for version in versions)
        or len(versions) != len(set(versions))
        or namespace_policy_version not in NAMESPACE_POLICIES
    ):
        raise ManifestPolicyError("manifest package_policy identity is invalid")
    try:
        specifier = SpecifierSet(requires_python)
        parsed_versions = tuple(Version(version) for version in versions)
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise ManifestPolicyError("manifest package_policy Python contract is invalid") from exc
    if any(version not in specifier for version in parsed_versions):
        raise ManifestPolicyError("manifest package_policy excludes a supported Python version")
    return PackageReleasePolicy(requires_python, tuple(versions), namespace_policy_version)


def discover_release_manifest(
    asset_dir: Path,
    *,
    release_tag: str | None = None,
) -> ManifestDiscovery:
    if not asset_dir.is_dir():
        raise ReleaseAssetError("release package directory is missing")
    core_paths = _package_files(asset_dir, "avibe-os", ".whl")
    memory_paths = _package_files(asset_dir, "avibe-memory", ".whl")
    if len(core_paths) != 1 or len(memory_paths) > 1:
        raise ReleaseAssetError("release must contain one core wheel and at most one Memory wheel")
    core = _wheel_archive(core_paths[0])
    core_manifest = _installed_files(core).get(MEMORY_MANIFEST_PATH)
    memory = _wheel_archive(memory_paths[0]) if memory_paths else None
    memory_manifest = _installed_files(memory).get(MEMORY_MANIFEST_PATH) if memory else None
    if core_manifest and memory_manifest:
        raise ReleaseAssetError("Memory Runtime manifest ownership is ambiguous")
    if memory_manifest:
        return ManifestDiscovery("memory", memory_manifest)
    transition_pin = _exact_memory_pin(core.metadata)
    if transition_pin is not None:
        detail = "artifact is missing" if memory is None else "artifact has no owned manifest"
        raise ReleaseAssetError(f"transition avibe-memory {detail}")
    if memory is not None:
        raise ReleaseAssetError("legacy release unexpectedly contains an unowned Memory wheel")
    if core_manifest:
        return ManifestDiscovery("core", core_manifest)
    if release_tag is not None and _release_version(release_tag) > LAST_LEGACY_RELEASE_VERSION:
        raise ReleaseAssetError("transition-and-later release is missing its Memory manifest")
    raise LegacyManifestAbsent("legacy release predates the Memory Runtime manifest")


def _assert_metadata_parity(wheel: PackageArchive, sdist: PackageArchive, name: str) -> None:
    if wheel.metadata.name != name or wheel.metadata != sdist.metadata:
        raise ReleaseAssetError(f"{name} wheel and sdist metadata differ")


def _owned_content(package: PackageArchive, prefix: str) -> dict[str, bytes]:
    return {name: value for name, value in _installed_files(package).items() if name.startswith(prefix)}


def _assert_namespace_policy(package: PackageArchive, distribution: str, policy_version: int) -> None:
    policies = NAMESPACE_POLICIES[policy_version]
    allowed_top_levels = policies[distribution]
    known_namespaces = set().union(*policies.values())
    for name in _installed_files(package):
        if name == MEMORY_MANIFEST_PATH:
            if distribution == "avibe-memory":
                continue
            raise ReleaseAssetError(f"{distribution} artifact violates namespace policy: {name}")
        top_level = PurePosixPath(name).parts[0]
        if top_level.endswith((".dist-info", ".data")):
            continue
        if package.path.suffix == ".whl":
            allowed = top_level in allowed_top_levels
        else:
            allowed = top_level not in known_namespaces or top_level in allowed_top_levels
        if not allowed:
            raise ReleaseAssetError(f"{distribution} artifact violates namespace policy: {name}")


def _assert_transition_packages(
    packages: tuple[PackageArchive, PackageArchive, PackageArchive, PackageArchive],
    policy: PackageReleasePolicy,
) -> str:
    core_wheel, memory_wheel, core_sdist, memory_sdist = packages
    _assert_metadata_parity(core_wheel, core_sdist, "avibe-os")
    _assert_metadata_parity(memory_wheel, memory_sdist, "avibe-memory")
    version = core_wheel.metadata.version
    if memory_wheel.metadata.version != version:
        raise ReleaseAssetError("core and Memory distribution versions differ")
    if _exact_memory_pin(core_wheel.metadata) != version:
        raise ReleaseAssetError("transition core must hard-depend on the exact Memory version")
    reverse_requirements = _requirements_for(memory_wheel.metadata, "avibe-os")
    if len(reverse_requirements) != 1:
        raise ReleaseAssetError("Memory metadata must contain exactly one avibe-os dependency")
    reverse_requirement = reverse_requirements[0]
    if (
        reverse_requirement.url is not None
        or reverse_requirement.marker is not None
        or not reverse_requirement.specifier
        or Version(version) not in reverse_requirement.specifier
    ):
        raise ReleaseAssetError("Memory avibe-os dependency must accept the exact core version")
    requires_python = {core_wheel.metadata.requires_python, memory_wheel.metadata.requires_python}
    if requires_python != {policy.requires_python}:
        raise ReleaseAssetError(f"core and Memory Requires-Python must match release policy {policy.requires_python}")

    for package, distribution in (
        (core_wheel, "avibe-os"),
        (core_sdist, "avibe-os"),
        (memory_wheel, "avibe-memory"),
        (memory_sdist, "avibe-memory"),
    ):
        _assert_namespace_policy(package, distribution, policy.namespace_policy_version)
    wheel_implementation = _owned_content(memory_wheel, "avibe_memory/")
    sdist_implementation = _owned_content(memory_sdist, "avibe_memory/")
    if not REQUIRED_MEMORY_IMPLEMENTATION.issubset(wheel_implementation):
        raise ReleaseAssetError("Memory artifact is missing required implementation files")
    if wheel_implementation != sdist_implementation:
        raise ReleaseAssetError("Memory wheel and sdist implementation content differ")
    wheel_manifest = _installed_files(memory_wheel).get(MEMORY_MANIFEST_PATH)
    sdist_manifest = _installed_files(memory_sdist).get(MEMORY_MANIFEST_PATH)
    if wheel_manifest is None or wheel_manifest != sdist_manifest:
        raise ReleaseAssetError("Memory wheel and sdist manifest content differ")
    return version


def _transition_artifacts(
    asset_dir: Path,
) -> tuple[PackageArchive, PackageArchive, PackageArchive, PackageArchive]:
    if not asset_dir.is_dir():
        raise ReleaseAssetError("release package directory is missing")

    def one(distribution: str, suffix: str) -> Path:
        matches = _package_files(asset_dir, distribution, suffix)
        if len(matches) != 1:
            raise ReleaseAssetError(f"release must contain one {distribution} {suffix}")
        return matches[0]

    return (
        _wheel_archive(one("avibe-os", ".whl")),
        _wheel_archive(one("avibe-memory", ".whl")),
        _sdist_archive(one("avibe-os", ".tar.gz")),
        _sdist_archive(one("avibe-memory", ".tar.gz")),
    )


def _extract_sdist(sdist: Path, destination: Path) -> Path:
    archive = _sdist_archive(sdist)
    project_root = destination / sdist.name.removesuffix(".tar.gz")
    project_root.mkdir(parents=True)
    for name, content in archive.files.items():
        target = project_root.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return project_root


def rebuild_sdist_wheel(sdist: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    attempt_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=output_dir))
    extract_dir = attempt_dir / "source"
    project_root = _extract_sdist(sdist, extract_dir)
    wheel_dir = attempt_dir / "wheel"
    wheel_dir.mkdir(parents=True)
    environment = {**os.environ, "PYTHONPATH": ""}
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheel_dir), str(project_root)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseAssetError(f"isolated sdist rebuild failed for {sdist.name}: {result.stdout}{result.stderr}")
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseAssetError(f"isolated sdist rebuild produced {len(wheels)} wheels for {sdist.name}")
    return wheels[0]


def _resolve_wheelhouse(
    wheels: tuple[Path, Path],
    find_links: tuple[Path, ...],
    python_versions: tuple[str, ...],
) -> None:
    for python_version in python_versions:
        links = [item for directory in find_links for item in ("--find-links", str(directory.resolve()))]
        command = [*PIP_FLAGS, "--python-version", python_version, *links]
        command.extend(str(wheel.resolve()) for wheel in wheels)
        _run_pip(command, f"offline pip resolution failed for Python {python_version}")


def verify_transition_distributions(
    asset_dir: Path,
    rebuild_root: Path,
    *,
    release_tag: str,
    builder: SdistBuilder = rebuild_sdist_wheel,
) -> str:
    staged = _transition_artifacts(asset_dir)
    core_wheel, memory_wheel, core_sdist, memory_sdist = staged
    manifest_bytes = _installed_files(memory_wheel).get(MEMORY_MANIFEST_PATH)
    try:
        manifest_payload = json.loads(manifest_bytes) if manifest_bytes is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError("Memory artifact package policy manifest is invalid") from exc
    if not isinstance(manifest_payload, dict):
        raise ReleaseAssetError("Memory artifact is missing its package policy manifest")
    policy = _package_release_policy(manifest_payload, release_tag)
    if policy is None:
        raise ReleaseAssetError("Memory artifact manifest is missing frozen package_policy")
    version = _assert_transition_packages(staged, policy)
    if Version(version) != _release_version(release_tag):
        raise ReleaseAssetError("distribution version does not match the release tag")
    _resolve_wheelhouse(
        (core_wheel.path, memory_wheel.path),
        (asset_dir,),
        policy.supported_python_versions,
    )
    rebuilt_core = _wheel_archive(builder(core_sdist.path, rebuild_root / "core"))
    rebuilt_memory = _wheel_archive(builder(memory_sdist.path, rebuild_root / "memory"))
    rebuilt_version = _assert_transition_packages((rebuilt_core, rebuilt_memory, core_sdist, memory_sdist), policy)
    if rebuilt_version != version:
        raise ReleaseAssetError("rebuilt distribution version differs from staged artifacts")
    _resolve_wheelhouse(
        (rebuilt_core.path, rebuilt_memory.path),
        (asset_dir, rebuilt_core.path.parent, rebuilt_memory.path.parent),
        policy.supported_python_versions,
    )
    return version


def load_release_spec(manifest_path: Path) -> ReleaseSpec:
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestPolicyError(f"cannot read Memory Runtime manifest: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManifestPolicyError("Memory Runtime manifest schema_version must be 1")
    everos_version = payload.get("everos_version")
    provenance = PUBLISHED_RUNTIME_PROVENANCE.get(everos_version) if isinstance(everos_version, str) else None
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
    _package_release_policy(payload, release_tag)
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
                        bundle.getmember("lib/python3.12/site-packages/avibe_memory_sync_bootstrap.pth")
                    )
                    if (
                        bootstrap_digest != spec.sync_bootstrap_sha256
                        or scrubbers_digest != spec.sync_scrubbers_sha256
                        or marker is None
                        or marker.read() != b"import avibe_memory_sync_bootstrap\n"
                    ):
                        raise ReleaseAssetError(f"Memory Runtime sync contract mismatch: {archive.name}")
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


def verify_public_release_assets(manifest_path: Path) -> ReleaseSpec:
    with tempfile.TemporaryDirectory(prefix="memory-runtime-public-verify-") as temporary:
        return fetch_release_assets(manifest_path, Path(temporary) / "assets")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch")
    fetch.add_argument("--output-dir", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--asset-dir", type=Path, required=True)
    discover = subparsers.add_parser("discover-manifest")
    discover.add_argument("--asset-dir", type=Path, required=True)
    discover.add_argument("--output-manifest", type=Path, required=True)
    discover.add_argument("--release-tag")
    packages = subparsers.add_parser("verify-packages")
    packages.add_argument("--asset-dir", type=Path, required=True)
    packages.add_argument("--work-dir", type=Path)
    subparsers.add_parser("verify-public")
    subparsers.add_parser("check-policy")
    args = parser.parse_args(argv)
    if args.command != "discover-manifest" and args.manifest is None:
        parser.error("--manifest is required for this command")
    result: dict[str, object] = {}
    try:
        if args.command == "fetch":
            spec = fetch_release_assets(args.manifest, args.output_dir)
        elif args.command == "verify":
            spec = verify_release_assets(args.manifest, args.asset_dir)
        elif args.command == "discover-manifest":
            discovery = discover_release_manifest(args.asset_dir, release_tag=args.release_tag)
            args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
            args.output_manifest.write_bytes(discovery.manifest_bytes)
            spec = load_release_spec(args.output_manifest)
            if args.release_tag and spec.release_tag != args.release_tag:
                raise ReleaseAssetError("owned manifest release identity does not match the selected release")
            result["manifest_owner"] = discovery.owner
        elif args.command == "verify-packages":
            spec = load_release_spec(args.manifest)
            if args.work_dir is None:
                with tempfile.TemporaryDirectory(prefix="memory-distribution-rebuild-") as temporary:
                    version = verify_transition_distributions(
                        args.asset_dir, Path(temporary), release_tag=spec.release_tag
                    )
            else:
                args.work_dir.mkdir(parents=True, exist_ok=True)
                version = verify_transition_distributions(args.asset_dir, args.work_dir, release_tag=spec.release_tag)
            discovery = discover_release_manifest(args.asset_dir, release_tag=spec.release_tag)
            if discovery.owner != "memory" or discovery.manifest_bytes != spec.manifest_bytes:
                raise ReleaseAssetError("transition package manifest does not match the selected manifest")
            result["package_version"] = version
        elif args.command == "verify-public":
            spec = verify_public_release_assets(args.manifest)
        else:
            spec = load_release_spec(args.manifest)
    except LegacyManifestAbsent as exc:
        print(
            json.dumps({"ok": False, "failure_kind": "legacy_absent", "error": str(exc)}),
            file=sys.stderr,
        )
        return LEGACY_MANIFEST_ABSENT_EXIT
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
    result.update(
        ok=True,
        release_tag=spec.release_tag,
        asset_count=len(spec.expected_asset_names),
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
