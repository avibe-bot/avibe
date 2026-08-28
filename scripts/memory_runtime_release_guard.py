#!/usr/bin/env python3
"""Verify and materialize manifest-pinned Memory Runtime release assets.

Identity and byte-level checks precede semantic parsing. Semantic policy parsers
never receive bytes that failed an available identity or exact-byte check.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import PathDistribution
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
class InstalledDistribution:
    metadata: PackageMetadata
    dist_info: str
    files: dict[tuple[str, str], bytes]
    python_version: str


@dataclass(frozen=True)
class PackageReleasePolicy:
    requires_python: str
    supported_python_versions: tuple[str, ...]
    namespace_policy_version: int


@dataclass(frozen=True)
class ManifestDiscovery:
    owner: str
    manifest_bytes: bytes


INSTALL_SCHEMES = ("purelib", "platlib", "scripts", "headers", "data")
PIP_OFFLINE_FLAGS = ("--disable-pip-version-check", "--no-input", "--no-index", "--no-cache-dir")
_CORE_FORBIDDEN = frozenset({"avibe_memory/", MEMORY_MANIFEST_PATH})
_MEMORY_FORBIDDEN = frozenset({"config/", "core/", "modules/", "storage/", "vibe/"})
FORBIDDEN_PATH_POLICIES = {
    1: {
        "avibe-os": {scheme: _CORE_FORBIDDEN for scheme in INSTALL_SCHEMES},
        "avibe-memory": {
            **{scheme: _MEMORY_FORBIDDEN for scheme in INSTALL_SCHEMES},
            "scripts": _MEMORY_FORBIDDEN | {"vibe"},
        },
    }
}


def _run_interpreter(
    python_executable: Path,
    arguments: list[str],
    failure: str,
) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_CACHE_DIR="1", PYTHONNOUSERSITE="1", PYTHONPATH="")
    try:
        executable = python_executable.expanduser().resolve(strict=True)
        result = subprocess.run(
            [str(executable), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ReleaseAssetError(f"requested Python interpreter is unavailable: {python_executable}: {exc}") from exc
    if result.returncode:
        raise ReleaseAssetError(f"{failure}: {result.stdout}{result.stderr}")
    return result


def _run_pip(python_executable: Path, arguments: list[str], failure: str) -> None:
    _run_interpreter(python_executable, ["-m", "pip", "--isolated", *arguments], failure)


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


def _metadata(message, context: str) -> PackageMetadata:
    values = [message.get_all(key) or [] for key in ("Name", "Version", "Requires-Python")]
    if any(len(field) != 1 or not field[0] for field in values):
        raise ReleaseAssetError(f"{context} metadata has invalid identity fields")
    name, version, requires_python = (field[0] for field in values)
    return PackageMetadata(
        name=str(canonicalize_name(name)),
        version=version,
        requires_python=requires_python,
        requires_dist=tuple(sorted(message.get_all("Requires-Dist") or [])),
    )


def _pip_scheme(python_executable: Path, prefix: Path, distribution: str) -> tuple[dict[str, Path], str]:
    script = (
        "import json,sys; from pip._internal.locations import get_scheme; "
        "s=get_scheme(sys.argv[1],prefix=sys.argv[2]); "
        "print(json.dumps({k:getattr(s,k) for k in "
        "('purelib','platlib','scripts','headers','data')} | "
        "{'python':f'{sys.version_info.major}.{sys.version_info.minor}'}))"
    )
    result = _run_interpreter(
        python_executable,
        ["-I", "-c", script, distribution, str(prefix.resolve())],
        f"cannot determine pip install scheme for {distribution}",
    )
    try:
        raw = json.loads(result.stdout)
        keys = {*INSTALL_SCHEMES, "python"}
        if not isinstance(raw, dict) or set(raw) != keys or not all(isinstance(value, str) for value in raw.values()):
            raise ValueError
    except (json.JSONDecodeError, ValueError) as exc:
        raise ReleaseAssetError(f"pip install scheme is invalid for {distribution}") from exc
    return ({key: Path(raw[key]).resolve() for key in INSTALL_SCHEMES}, raw["python"])


def _pip_install(
    python_executable: Path,
    wheels: tuple[Path, ...],
    find_links: tuple[Path, ...],
    prefix: Path,
    *,
    no_deps: bool,
) -> None:
    prefix.mkdir(parents=True)
    links = [item for directory in find_links for item in ("--find-links", str(directory.resolve()))]
    arguments = [
        "install",
        *PIP_OFFLINE_FLAGS,
        "--no-compile",
        "--ignore-installed",
        "--only-binary=:all:",
        "--prefix",
        str(prefix.resolve()),
        *links,
    ]
    if no_deps:
        arguments.append("--no-deps")
    arguments.extend(str(wheel.resolve()) for wheel in wheels)
    _run_pip(python_executable, arguments, "offline pip install failed")


def _installed_distribution(
    wheel: Path,
    distribution: str,
    prefix: Path,
    python_executable: Path,
) -> InstalledDistribution:
    _pip_install(python_executable, (wheel,), (wheel.parent,), prefix, no_deps=True)
    metadata_paths = sorted(prefix.rglob("*.dist-info/METADATA"))
    if len(metadata_paths) != 1:
        raise ReleaseAssetError(f"pip install produced {len(metadata_paths)} METADATA files for {wheel.name}")
    metadata_path = metadata_paths[0]
    installed = PathDistribution(metadata_path.parent)
    metadata = _metadata(installed.metadata, wheel.name)
    if metadata.name != canonicalize_name(distribution):
        raise ReleaseAssetError(f"installed distribution identity differs from expected {distribution}")
    _assert_filename_identity(wheel, metadata)
    scheme, python_version = _pip_scheme(python_executable, prefix, distribution)
    scheme_roots = sorted(
        scheme.items(),
        key=lambda item: (-len(item[1].parts), INSTALL_SCHEMES.index(item[0])),
    )
    entries = installed.files
    if entries is None:
        raise ReleaseAssetError(f"installed RECORD is invalid: {wheel.name}")
    files: dict[tuple[str, str], bytes] = {}
    recorded: set[Path] = set()
    for entry in entries:
        target = Path(entry.locate()).resolve()
        try:
            target.relative_to(prefix.resolve())
        except ValueError as exc:
            raise ReleaseAssetError(f"installed RECORD escapes its disposable prefix: {entry}") from exc
        if target in recorded or not target.is_file() or target.is_symlink():
            raise ReleaseAssetError(f"installed RECORD path is invalid: {entry}")
        recorded.add(target)
        for scheme_name, root in scheme_roots:
            try:
                relative = target.relative_to(root)
            except ValueError:
                continue
            key = (scheme_name, relative.as_posix())
            if not relative.parts or key in files:
                raise ReleaseAssetError(f"installed RECORD has a colliding path: {entry}")
            files[key] = target.read_bytes()
            break
        else:
            raise ReleaseAssetError(f"installed path is outside pip's install scheme: {entry}")
    actual = {path.resolve() for path in prefix.rglob("*") if path.is_file()}
    if actual != recorded:
        raise ReleaseAssetError(f"installed inventory differs from RECORD: {wheel.name}")
    return InstalledDistribution(metadata, metadata_path.parent.name, files, python_version)


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
        or "*" in specifiers[0].version
    ):
        return None
    try:
        return str(Version(specifiers[0].version))
    except InvalidVersion as exc:
        raise ReleaseAssetError("avibe-memory exact dependency has an invalid version") from exc


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
        or namespace_policy_version not in FORBIDDEN_PATH_POLICIES
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
    python_executable: Path,
    release_tag: str | None = None,
) -> ManifestDiscovery:
    if not asset_dir.is_dir():
        raise ReleaseAssetError("release package directory is missing")
    core_paths = _package_files(asset_dir, "avibe-os", ".whl")
    memory_paths = _package_files(asset_dir, "avibe-memory", ".whl")
    if len(core_paths) != 1 or len(memory_paths) > 1:
        raise ReleaseAssetError("release must contain one core wheel and at most one Memory wheel")
    with tempfile.TemporaryDirectory(prefix="memory-manifest-discovery-") as temporary:
        root = Path(temporary)
        core = _installed_distribution(core_paths[0], "avibe-os", root / "core", python_executable)
        memory = (
            _installed_distribution(memory_paths[0], "avibe-memory", root / "memory", python_executable)
            if memory_paths
            else None
        )
        core_manifest = _distribution_resource(core, MEMORY_MANIFEST_PATH)
        memory_manifest = _distribution_resource(memory, MEMORY_MANIFEST_PATH) if memory else None
    if core_manifest and memory_manifest:
        raise ReleaseAssetError("Memory Runtime manifest ownership is ambiguous")
    if memory_manifest:
        return ManifestDiscovery("memory", memory_manifest[1])
    transition_pin = _exact_memory_pin(core.metadata)
    if transition_pin is not None:
        detail = "artifact is missing" if memory is None else "artifact has no owned manifest"
        raise ReleaseAssetError(f"transition avibe-memory {detail}")
    if memory is not None:
        raise ReleaseAssetError("legacy release unexpectedly contains an unowned Memory wheel")
    if core_manifest:
        return ManifestDiscovery("core", core_manifest[1])
    if release_tag is not None and _release_version(release_tag) > LAST_LEGACY_RELEASE_VERSION:
        raise ReleaseAssetError("transition-and-later release is missing its Memory manifest")
    raise LegacyManifestAbsent("legacy release predates the Memory Runtime manifest")


def _distribution_resource(package: InstalledDistribution, path: str) -> tuple[str, bytes] | None:
    matches = [(scheme, content) for (scheme, name), content in package.files.items() if name == path]
    if len(matches) > 1:
        raise ReleaseAssetError(f"distribution resource has multiple installed locations: {path}")
    return matches[0] if matches else None


def _owned_files(package: InstalledDistribution) -> dict[tuple[str, str], bytes]:
    return {
        key: value
        for key, value in package.files.items()
        if not (
            key[0] in {"purelib", "platlib"}
            and (key[1] == package.dist_info or key[1].startswith(f"{package.dist_info}/"))
        )
    }


def _assert_forbidden_paths(
    package: InstalledDistribution,
    distribution: str,
    policy_version: int,
) -> None:
    policy = FORBIDDEN_PATH_POLICIES[policy_version][distribution]
    for key in _owned_files(package):
        scheme, path = key
        if distribution == "avibe-memory" and path == MEMORY_MANIFEST_PATH:
            continue
        if any(path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in policy.get(scheme, ())):
            raise ReleaseAssetError(f"{distribution} artifact violates forbidden path policy: {scheme}/{path}")


def _assert_transition_pair(
    core: InstalledDistribution,
    memory: InstalledDistribution,
    policy: PackageReleasePolicy,
) -> str:
    version = core.metadata.version
    normalized_version = str(Version(version))
    if memory.metadata.version != version:
        raise ReleaseAssetError("core and Memory distribution versions differ")
    if _exact_memory_pin(core.metadata) != normalized_version:
        raise ReleaseAssetError("transition core must hard-depend on the exact Memory version")
    reverse_requirements = _requirements_for(memory.metadata, "avibe-os")
    if len(reverse_requirements) != 1:
        raise ReleaseAssetError("Memory metadata must contain exactly one avibe-os dependency")
    reverse_requirement = reverse_requirements[0]
    if (
        reverse_requirement.url is not None
        or reverse_requirement.marker is not None
        or not reverse_requirement.specifier
        or Version(normalized_version) not in reverse_requirement.specifier
    ):
        raise ReleaseAssetError("Memory avibe-os dependency must accept the exact core version")
    requires_python = {core.metadata.requires_python, memory.metadata.requires_python}
    if requires_python != {policy.requires_python}:
        raise ReleaseAssetError(f"core and Memory Requires-Python must match release policy {policy.requires_python}")
    _assert_forbidden_paths(core, "avibe-os", policy.namespace_policy_version)
    _assert_forbidden_paths(memory, "avibe-memory", policy.namespace_policy_version)
    return version


def _transition_artifacts(asset_dir: Path) -> tuple[Path, Path, Path, Path]:
    if not asset_dir.is_dir():
        raise ReleaseAssetError("release package directory is missing")

    def one(distribution: str, suffix: str) -> Path:
        matches = _package_files(asset_dir, distribution, suffix)
        if len(matches) != 1:
            raise ReleaseAssetError(f"release must contain one {distribution} {suffix}")
        return matches[0]

    return (
        one("avibe-os", ".whl"),
        one("avibe-memory", ".whl"),
        one("avibe-os", ".tar.gz"),
        one("avibe-memory", ".tar.gz"),
    )


def rebuild_sdist_wheel(
    sdist: Path,
    output_dir: Path,
    *,
    python_executable: Path,
    find_links: tuple[Path, ...],
) -> Path:
    distribution, _ = _filename_identity(sdist)
    output_dir.mkdir(parents=True, exist_ok=True)
    wheel_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=output_dir)) / "wheel"
    wheel_dir.mkdir(parents=True)
    links = [item for directory in find_links for item in ("--find-links", str(directory.resolve()))]
    _run_pip(
        python_executable,
        [
            "wheel",
            *PIP_OFFLINE_FLAGS,
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir.resolve()),
            *links,
            str(sdist.resolve()),
        ],
        f"isolated pip sdist rebuild failed for {sdist.name}",
    )
    wheels = _package_files(wheel_dir, distribution, ".whl")
    if len(wheels) != 1:
        raise ReleaseAssetError(f"isolated sdist rebuild produced {len(wheels)} wheels for {sdist.name}")
    return wheels[0]


def _install_pair(
    wheels: tuple[Path, Path],
    root: Path,
    python_executable: Path,
) -> tuple[InstalledDistribution, InstalledDistribution]:
    return (
        _installed_distribution(wheels[0], "avibe-os", root / "core", python_executable),
        _installed_distribution(wheels[1], "avibe-memory", root / "memory", python_executable),
    )


def verify_transition_distributions(
    asset_dir: Path,
    rebuild_root: Path,
    *,
    release_tag: str,
    python_executable: Path,
    expected_manifest: bytes | None = None,
) -> str:
    core_wheel, memory_wheel, core_sdist, memory_sdist = _transition_artifacts(asset_dir)
    rebuild_root.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=rebuild_root))
    staged = _install_pair((core_wheel, memory_wheel), attempt / "staged", python_executable)
    manifest_resource = _distribution_resource(staged[1], MEMORY_MANIFEST_PATH)
    manifest_bytes = manifest_resource[1] if manifest_resource else None
    if expected_manifest is not None and manifest_bytes != expected_manifest:
        raise ReleaseAssetError("transition package manifest does not match the selected manifest")
    try:
        manifest_payload = json.loads(manifest_bytes) if manifest_bytes is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError("Memory artifact package policy manifest is invalid") from exc
    if not isinstance(manifest_payload, dict):
        raise ReleaseAssetError("Memory artifact is missing its package policy manifest")
    policy = _package_release_policy(manifest_payload, release_tag)
    if policy is None:
        raise ReleaseAssetError("Memory artifact manifest is missing frozen package_policy")
    if staged[0].python_version not in policy.supported_python_versions:
        raise ReleaseAssetError(
            f"requested Python {staged[0].python_version} is not in the release policy interpreter matrix"
        )
    version = _assert_transition_pair(*staged, policy)
    if Version(version) != _release_version(release_tag):
        raise ReleaseAssetError("distribution version does not match the release tag")
    rebuilt_wheels = (
        rebuild_sdist_wheel(
            core_sdist, attempt / "core-build", python_executable=python_executable, find_links=(asset_dir,)
        ),
        rebuild_sdist_wheel(
            memory_sdist, attempt / "memory-build", python_executable=python_executable, find_links=(asset_dir,)
        ),
    )
    rebuilt = _install_pair(rebuilt_wheels, attempt / "rebuilt", python_executable)
    _assert_filename_identity(core_sdist, rebuilt[0].metadata)
    _assert_filename_identity(memory_sdist, rebuilt[1].metadata)
    rebuilt_version = _assert_transition_pair(*rebuilt, policy)
    if rebuilt_version != version:
        raise ReleaseAssetError("rebuilt distribution version differs from staged artifacts")
    for distribution, staged_package, rebuilt_package in zip(
        ("avibe-os", "avibe-memory"), staged, rebuilt, strict=True
    ):
        if staged_package.metadata != rebuilt_package.metadata:
            raise ReleaseAssetError(f"{distribution} wheel and sdist metadata differ")
        if _owned_files(staged_package) != _owned_files(rebuilt_package):
            raise ReleaseAssetError(f"{distribution} wheel and sdist installed content differ")
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
    discover.add_argument("--python-executable", type=Path, required=True)
    packages = subparsers.add_parser("verify-packages")
    packages.add_argument("--asset-dir", type=Path, required=True)
    packages.add_argument("--work-dir", type=Path)
    packages.add_argument("--python-executable", type=Path, required=True)
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
            discovery = discover_release_manifest(
                args.asset_dir,
                python_executable=args.python_executable,
                release_tag=args.release_tag,
            )
            args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
            args.output_manifest.write_bytes(discovery.manifest_bytes)
            spec = load_release_spec(args.output_manifest)
            if args.release_tag and spec.release_tag != args.release_tag:
                raise ReleaseAssetError("owned manifest release identity does not match the selected release")
            result["manifest_owner"] = discovery.owner
        elif args.command == "verify-packages":
            spec = load_release_spec(args.manifest)
            with tempfile.TemporaryDirectory(prefix="memory-distribution-rebuild-") as temporary:
                version = verify_transition_distributions(
                    args.asset_dir,
                    args.work_dir or Path(temporary),
                    release_tag=spec.release_tag,
                    python_executable=args.python_executable,
                    expected_manifest=spec.manifest_bytes,
                )
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
