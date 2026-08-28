#!/usr/bin/env python3
"""Verify and materialize manifest-pinned Memory Runtime release assets.

Package policy schema v1 declares ``Requires-Python: >=3.10``, supported minors
``3.10``, ``3.11``, and ``3.12``, and universal wheel tag ``py3-none-any``.
Changing a contract requires a policy schema/version change and an updated
repository-owned declaration.

Wheel archive validation retains O(one bounded member + control metadata),
never O(the archive's total decompressed payload).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
from email.parser import Parser
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath, PureWindowsPath
import zipfile

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
PACKAGE_POLICY_REQUIRES_PYTHON = ">=3.10"
PACKAGE_POLICY_SUPPORTED_PYTHON_VERSIONS = ("3.10", "3.11", "3.12")
SUPPORTED_NAMESPACE_POLICY_VERSIONS = frozenset({1})
MAX_WHEEL_MEMBER_BYTES = 16 * 1024 * 1024
WHEEL_DATA_SCHEMES = frozenset({"data", "headers", "platlib", "purelib", "scripts"})
WHEEL_RECORD_HASH_ALGORITHMS = frozenset({"sha256", "sha384", "sha512"})


@dataclass(frozen=True)
class _WheelControlPolicy:
    controls: tuple[tuple[str, int], ...]
    wheel_version_major: int
    metadata_version: str
    root_is_purelib: tuple[str, ...]
    tags: tuple[str, ...]
    parser_defects: tuple[str, ...]
    parser_body: str


_WHEEL_CONTROL_POLICY = _WheelControlPolicy(
    controls=(("METADATA", 1), ("WHEEL", 1)),
    wheel_version_major=1,
    metadata_version="2.4",
    root_is_purelib=("true",),
    tags=("py3-none-any",),
    parser_defects=(),
    parser_body="",
)


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
    version: str
    requires_python: str
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class PackageReleasePolicy:
    requires_python: str
    supported_python_versions: tuple[str, ...]
    wheel_tag: str
    namespace_policy_version: int


@dataclass(frozen=True)
class RequirementClassification:
    name: str
    specifier: str
    exact_version: str | None
    has_extras: bool
    has_marker: bool
    is_direct: bool


def _packaging_modules():
    try:
        from packaging import metadata, requirements, specifiers, tags, utils, version
    except ModuleNotFoundError as exc:
        raise ReleaseAssetError("static release verification requires packaging") from exc
    return metadata, requirements, specifiers, tags, utils, version


def _release_version(release_tag: str) -> str:
    *_, versions = _packaging_modules()
    try:
        return str(versions.Version(package_version_from_release_tag(release_tag)))
    except (ValueError, versions.InvalidVersion) as exc:
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
    if type(payload.get("release_tag")) is not str or payload["release_tag"] != release_tag:
        raise ManifestPolicyError("manifest release_tag differs from the selected release")
    raw = payload.get("package_policy")
    keys = {
        "schema_version",
        "release_tag",
        "release_family",
        "requires_python",
        "supported_python_versions",
        "wheel_tag",
        "namespace_policy_version",
    }
    if type(raw) is not dict or set(raw) != keys:
        raise ManifestPolicyError("manifest package_policy schema is invalid")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != PACKAGE_POLICY_SCHEMA_VERSION:
        raise ManifestPolicyError("manifest package_policy schema_version must be integer 1")
    if any(type(raw[key]) is not str or not raw[key] for key in ("release_tag", "release_family", "requires_python", "wheel_tag")):
        raise ManifestPolicyError("manifest package_policy string fields are invalid")
    versions = raw["supported_python_versions"]
    namespace_version = raw["namespace_policy_version"]
    if (
        type(versions) is not list
        or not versions
        or any(type(version) is not str or re.fullmatch(r"[0-9]+\.[0-9]+", version) is None for version in versions)
        or type(namespace_version) is not int
        or namespace_version not in SUPPORTED_NAMESPACE_POLICY_VERSIONS
    ):
        raise ManifestPolicyError("manifest package_policy typed fields are invalid")
    if tuple(versions) != PACKAGE_POLICY_SUPPORTED_PYTHON_VERSIONS:
        raise ManifestPolicyError("manifest supported Python versions differ from schema 1 policy")
    release_parts = _release_version(release_tag).split(".")
    if raw["release_tag"] != release_tag or raw["release_family"] != ".".join(release_parts[:2]):
        raise ManifestPolicyError("manifest package_policy release identity is invalid")
    _, _, specifiers, _, _, _ = _packaging_modules()
    try:
        requires_python = str(specifiers.SpecifierSet(raw["requires_python"]))
    except specifiers.InvalidSpecifier as exc:
        raise ManifestPolicyError("manifest package_policy Python contract is invalid") from exc
    if requires_python != PACKAGE_POLICY_REQUIRES_PYTHON:
        raise ManifestPolicyError("manifest package_policy Requires-Python differs from schema 1 policy")
    if raw["wheel_tag"] != _WHEEL_CONTROL_POLICY.tags[0]:
        raise ManifestPolicyError("manifest package_policy wheel_tag differs from schema 1 policy")
    return PackageReleasePolicy(requires_python, tuple(versions), raw["wheel_tag"], namespace_version)


def classify_requirement(raw: str) -> RequirementClassification:
    """Classify one valid requirement without treating wildcard equality as exact."""
    _, requirements, _, _, utils, versions = _packaging_modules()
    try:
        requirement = requirements.Requirement(raw)
    except requirements.InvalidRequirement as exc:
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
            exact_version = str(versions.Version(specifiers[0].version))
        except versions.InvalidVersion as exc:
            raise ReleaseAssetError(f"invalid exact requirement version: {raw!r}") from exc
    return RequirementClassification(str(utils.canonicalize_name(requirement.name)), str(requirement.specifier), exact_version, bool(requirement.extras), requirement.marker is not None, requirement.url is not None)


def _verify_package_identity(
    wheel_filename: str,
    metadata: PackageMetadata,
    *,
    expected_name: str,
    expected_version: str,
    expected_wheel_tag: str,
) -> None:
    """Bind an A2-supplied metadata record to filename and declared policy."""
    _, _, _, tags, utils, versions = _packaging_modules()
    if (
        type(metadata) is not PackageMetadata
        or type(wheel_filename) is not str
        or type(metadata.name) is not str
        or type(metadata.version) is not str
        or type(metadata.requires_python) is not str
        or type(metadata.requires_dist) is not tuple
        or any(type(requirement) is not str for requirement in metadata.requires_dist)
    ):
        raise ReleaseAssetError("transition package metadata types are invalid")
    try:
        filename_name, filename_version, _, filename_tags = utils.parse_wheel_filename(wheel_filename)
        metadata_name = str(utils.canonicalize_name(metadata.name))
        metadata_version = str(versions.Version(metadata.version))
        policy_tags = tags.parse_tag(expected_wheel_tag)
    except (TypeError, ValueError, utils.InvalidWheelFilename, versions.InvalidVersion) as exc:
        raise ReleaseAssetError(f"transition package identity is invalid: {wheel_filename!r}") from exc
    if metadata_name != str(filename_name) or metadata_version != str(filename_version):
        raise ReleaseAssetError(f"wheel filename identity differs from metadata: {wheel_filename}")
    if metadata_name != expected_name:
        raise ReleaseAssetError("transition wheel distribution identity is invalid")
    if metadata_version != expected_version:
        raise ReleaseAssetError("wheel metadata version does not match the release tag")
    if filename_tags != policy_tags:
        raise ReleaseAssetError(f"wheel tags differ from release policy: {wheel_filename}")


def _parse_wheel_version_major(values: tuple[str, ...]) -> int | None:
    if len(values) != 1:
        return None
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)", values[0])
    return int(match.group(1)) if match is not None else None


def _compare_wheel_control_policy(wheel: Path, observed: dict[str, object]) -> None:
    policy_fields = tuple(field.name for field in fields(_WHEEL_CONTROL_POLICY))
    if tuple(observed) not in (("controls",), policy_fields):
        raise ReleaseAssetError(f"wheel control policy observation is incomplete: {wheel.name}")
    mismatches = tuple(
        field.name
        for field in fields(_WHEEL_CONTROL_POLICY)
        if field.name in observed
        and observed[field.name] != getattr(_WHEEL_CONTROL_POLICY, field.name)
    )
    if mismatches:
        raise ReleaseAssetError(
            f"wheel control policy mismatch ({', '.join(mismatches)}): {wheel.name}"
        )


def _is_safe_wheel_path(value: str) -> bool:
    path = PurePosixPath(value)
    canonical = str(path) + ("/" if value.endswith("/") else "")
    return (
        bool(path.parts)
        and "\x00" not in value
        and "\\" not in value
        and not path.is_absolute()
        and not PureWindowsPath(value).drive
        and ".." not in path.parts
        and value == canonical
    )


def _read_wheel_member(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo, wheel: Path
) -> bytes:
    if member.file_size < 0 or member.file_size > MAX_WHEEL_MEMBER_BYTES:
        raise ReleaseAssetError(f"wheel member size is invalid: {wheel.name}")
    try:
        with archive.open(member) as stream:
            content = stream.read(MAX_WHEEL_MEMBER_BYTES + 1)
    except Exception as exc:
        raise ReleaseAssetError(f"cannot read wheel member: {wheel.name}") from exc
    if len(content) > MAX_WHEEL_MEMBER_BYTES or len(content) != member.file_size:
        raise ReleaseAssetError(f"wheel member size is invalid: {wheel.name}")
    return content


def _valid_record_hash(value: str) -> bool:
    algorithm, separator, encoded = value.partition("=")
    if separator != "=" or algorithm not in WHEEL_RECORD_HASH_ALGORITHMS or not encoded or "=" in encoded:
        return False
    if re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
        return False
    try:
        decoded = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        return False
    return len(decoded) == hashlib.new(algorithm).digest_size


def _validate_wheel_archive(
    archive: zipfile.ZipFile, wheel: Path, dist_info: str
) -> dict[str, bytes]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    aliases = [name.rstrip("/").casefold() for name in names]
    data_root = f"{dist_info.removesuffix('.dist-info')}.data"
    unsafe_data = any(
        parts[0].endswith(".data")
        and (parts[0] != data_root or len(parts) < 3 or parts[1] not in WHEEL_DATA_SCHEMES or not parts[2])
        for name in names
        for parts in (name.split("/"),)
    )
    if any(not _is_safe_wheel_path(name) for name in names) or len(aliases) != len(set(aliases)) or unsafe_data:
        raise ReleaseAssetError(f"wheel archive structure is invalid: {wheel.name}")
    record = f"{dist_info}/RECORD"
    retained = {record, *(f"{dist_info}/{name}" for name, _ in _WHEEL_CONTROL_POLICY.controls)}
    member_names = {info.filename for info in infos if not info.is_dir()}
    members = {}
    for info in infos:
        if not info.is_dir():
            content = _read_wheel_member(archive, info, wheel)
            if info.filename in retained:
                members[info.filename] = content
    if names.count(record) != 1:
        raise ReleaseAssetError(f"wheel RECORD structure is invalid: {wheel.name}")
    try:
        rows = list(csv.reader(io.StringIO(members[record].decode("utf-8")), strict=True))
    except Exception as exc:
        raise ReleaseAssetError(f"wheel RECORD structure is invalid: {wheel.name}") from exc
    generated = {record, f"{record}.jws", f"{record}.p7s"}
    recorded: set[str] = set()
    recorded_aliases: set[str] = set()
    for row in rows:
        if len(row) != 3 or not _is_safe_wheel_path(row[0]):
            raise ReleaseAssetError(f"wheel RECORD structure is invalid: {wheel.name}")
        path, digest, size = row
        alias = path.casefold()
        if path in recorded or alias in recorded_aliases:
            raise ReleaseAssetError(f"wheel RECORD structure is invalid: {wheel.name}")
        if path in generated:
            valid_fields = not digest and not size
        else:
            valid_fields = _valid_record_hash(digest) and re.fullmatch(r"[0-9]+", size) is not None
        if not valid_fields:
            raise ReleaseAssetError(f"wheel RECORD structure is invalid: {wheel.name}")
        recorded.add(path)
        recorded_aliases.add(alias)
    if record not in recorded or recorded - generated != member_names - generated:
        raise ReleaseAssetError(f"wheel RECORD entries differ from archive: {wheel.name}")
    return members


def inspect_wheel(wheel: Path, *, policy: PackageReleasePolicy) -> PackageMetadata:
    """Validate wheel control metadata without inferring installed behavior."""
    if (
        not isinstance(wheel, Path)
        or type(policy) is not PackageReleasePolicy
        or policy.requires_python != PACKAGE_POLICY_REQUIRES_PYTHON
        or policy.supported_python_versions != PACKAGE_POLICY_SUPPORTED_PYTHON_VERSIONS
        or policy.wheel_tag != _WHEEL_CONTROL_POLICY.tags[0]
        or type(policy.namespace_policy_version) is not int
        or policy.namespace_policy_version not in SUPPORTED_NAMESPACE_POLICY_VERSIONS
    ):
        raise ReleaseAssetError("wheel inspection policy is invalid")
    metadata_module, _, _, tags, utils, _ = _packaging_modules()
    try:
        filename_name, filename_version, _, filename_tags = utils.parse_wheel_filename(wheel.name)
        policy_tags = tags.parse_tag(policy.wheel_tag)
    except (TypeError, ValueError, utils.InvalidWheelFilename) as exc:
        raise ReleaseAssetError(f"invalid wheel filename: {wheel.name}") from exc
    if filename_tags != policy_tags:
        raise ReleaseAssetError(f"wheel tags differ from release policy: {wheel.name}")
    dist_info = f"{str(filename_name).replace('-', '_')}-{filename_version}.dist-info"
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            dist_infos = {
                name.split("/", 1)[0]
                for name in names
                if "/" in name and name.split("/", 1)[0].endswith(".dist-info")
            }
            if dist_infos != {dist_info}:
                raise ReleaseAssetError(f"wheel control structure is invalid: {wheel.name}")
            control_counts = tuple(
                (name, names.count(f"{dist_info}/{name}"))
                for name, _ in _WHEEL_CONTROL_POLICY.controls
            )
            _compare_wheel_control_policy(wheel, {"controls": control_counts})
            members = _validate_wheel_archive(archive, wheel, dist_info)
            metadata_bytes, wheel_bytes = (
                members[f"{dist_info}/{name}"]
                for name, _ in _WHEEL_CONTROL_POLICY.controls
            )
    except ReleaseAssetError:
        raise
    except Exception as exc:
        raise ReleaseAssetError(f"cannot read wheel controls: {wheel.name}") from exc
    try:
        parsed_metadata = metadata_module.Metadata.from_email(metadata_bytes, validate=True)
        wheel_metadata = Parser().parsestr(wheel_bytes.decode("utf-8"))
        wheel_versions = tuple(wheel_metadata.get_all("Wheel-Version") or ())
        observation = {
            "controls": control_counts,
            "wheel_version_major": _parse_wheel_version_major(wheel_versions),
            "metadata_version": parsed_metadata.metadata_version,
            "root_is_purelib": tuple(wheel_metadata.get_all("Root-Is-Purelib") or ()),
            "tags": tuple(wheel_metadata.get_all("Tag") or ()),
            "parser_defects": tuple(type(defect).__name__ for defect in wheel_metadata.defects),
            "parser_body": wheel_metadata.get_payload(),
        }
        _compare_wheel_control_policy(wheel, observation)
    except ReleaseAssetError:
        raise
    except Exception as exc:
        raise ReleaseAssetError(f"wheel control metadata is invalid: {wheel.name}") from exc
    platforms = {tag.platform for tag in policy_tags}
    for supported_version in policy.supported_python_versions:
        python_version = tuple(map(int, supported_version.split(".")))
        compatible = tags.compatible_tags(
            python_version,
            interpreter=f"cp{python_version[0]}{python_version[1]}",
            platforms=platforms,
        )
        if policy_tags.isdisjoint(compatible):
            raise ReleaseAssetError(
                f"wheel tags do not cover supported Python {supported_version}: {wheel.name}"
            )
    if parsed_metadata.requires_python is None:
        raise ReleaseAssetError(f"wheel metadata identity is invalid: {wheel.name}")
    package_metadata = PackageMetadata(
        str(utils.canonicalize_name(parsed_metadata.name)),
        str(parsed_metadata.version),
        str(parsed_metadata.requires_python),
        tuple(str(requirement) for requirement in parsed_metadata.requires_dist or []),
    )
    if package_metadata.name != str(filename_name) or package_metadata.version != str(filename_version):
        raise ReleaseAssetError(f"wheel filename identity differs from metadata: {wheel.name}")
    return package_metadata


def _requirements_for(metadata: PackageMetadata, name: str) -> tuple[RequirementClassification, ...]:
    _, _, _, _, utils, _ = _packaging_modules()
    expected = str(utils.canonicalize_name(name))
    classified = tuple(classify_requirement(raw) for raw in metadata.requires_dist)
    return tuple(item for item in classified if item.name == expected)


def verify_static_transition(
    *,
    core_wheel_filename: str,
    core_metadata: PackageMetadata,
    memory_wheel_filename: str,
    memory_metadata: PackageMetadata,
    release_tag: str,
    manifest_bytes: bytes,
    expected_manifest: bytes,
) -> tuple[PackageMetadata, PackageMetadata, PackageReleasePolicy]:
    """Freeze A1's policy interface for A2-supplied wheel metadata."""
    policy = load_package_release_policy(manifest_bytes, expected_manifest=expected_manifest, release_tag=release_tag)
    expected_version = _release_version(release_tag)
    _verify_package_identity(
        core_wheel_filename,
        core_metadata,
        expected_name="avibe-os",
        expected_version=expected_version,
        expected_wheel_tag=policy.wheel_tag,
    )
    _verify_package_identity(
        memory_wheel_filename,
        memory_metadata,
        expected_name="avibe-memory",
        expected_version=expected_version,
        expected_wheel_tag=policy.wheel_tag,
    )
    if core_metadata.requires_python != policy.requires_python or memory_metadata.requires_python != policy.requires_python:
        raise ReleaseAssetError(f"wheel Requires-Python must match release policy {policy.requires_python}")
    memory_dependencies = _requirements_for(core_metadata, "avibe-memory")
    if len(memory_dependencies) != 1 or memory_dependencies[0].exact_version != expected_version:
        raise ReleaseAssetError("transition core must hard-depend on the exact Memory version")
    core_dependencies = _requirements_for(memory_metadata, "avibe-os")
    if len(core_dependencies) != 1:
        raise ReleaseAssetError("Memory metadata must contain exactly one avibe-os dependency")
    reverse = core_dependencies[0]
    if reverse.exact_version != expected_version:
        raise ReleaseAssetError("Memory must hard-depend on the exact avibe-os release version")
    return core_metadata, memory_metadata, policy


def verify_wheel_transition(
    core_wheel: Path,
    memory_wheel: Path,
    *,
    release_tag: str,
    manifest_bytes: bytes,
    expected_manifest: bytes,
) -> tuple[PackageMetadata, PackageMetadata, PackageReleasePolicy]:
    """Compose A2a wheel controls after A1's exact manifest identity check."""
    policy = load_package_release_policy(
        manifest_bytes,
        expected_manifest=expected_manifest,
        release_tag=release_tag,
    )
    core_metadata = inspect_wheel(core_wheel, policy=policy)
    memory_metadata = inspect_wheel(memory_wheel, policy=policy)
    return verify_static_transition(
        core_wheel_filename=core_wheel.name,
        core_metadata=core_metadata,
        memory_wheel_filename=memory_wheel.name,
        memory_metadata=memory_metadata,
        release_tag=release_tag,
        manifest_bytes=manifest_bytes,
        expected_manifest=expected_manifest,
    )


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
