"""Exact package capture, persisted recovery DTOs, and rollback resolution."""

from __future__ import annotations

import email.parser
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidName,
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from vibe.runtime import ServiceLauncher


CORE_PACKAGE_NAME = "avibe-os"
LEGACY_CORE_PACKAGE_NAME = "vibe-remote"
MEMORY_PACKAGE_NAME = "avibe-memory"
CORE_DISTRIBUTION_NAMES = frozenset({CORE_PACKAGE_NAME, LEGACY_CORE_PACKAGE_NAME})
MEMORY_SPLIT_MIN_VERSION = Version("3.0.14.dev0")
PACKAGE_SHAPE_RECORD_SCHEMA_VERSION = 1


class PackageShapeError(ValueError):
    """The installed package shape cannot be represented safely."""


class DuplicateDistributionProviderError(PackageShapeError):
    """More than one dist-info provider claims one canonical distribution."""


class RollbackResolutionError(PackageShapeError):
    """An exact rollback target could not be resolved entirely into staging."""


class PackageShapeRecordError(PackageShapeError):
    """Persisted package-shape data is unknown, incomplete, or inconsistent."""


class ReleaseFamily(str, Enum):
    PRE_SPLIT = "pre_split"
    OPTIONAL_SPLIT = "optional_split"
    TRANSITION = "transition"


def _canonical_distribution_name(
    value: object,
    *,
    field: str,
    error_type: type[PackageShapeError] = PackageShapeError,
    require_canonical: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{field} name is missing")
    try:
        canonical = canonicalize_name(value, validate=True)
    except InvalidName as exc:
        raise error_type(f"{field} name is invalid") from exc
    if require_canonical and value != canonical:
        raise error_type(f"{field} is not canonical")
    return canonical


def _normalized_version(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackageShapeError(f"{field} is missing")
    try:
        return str(Version(value.strip()))
    except InvalidVersion as exc:
        raise PackageShapeError(f"{field} is not a valid package version") from exc


def _normalized_provider_id(value: object) -> str:
    try:
        provider_id = os.fsdecode(os.fspath(value)).strip()
    except TypeError as exc:
        raise PackageShapeError("distribution provider identity is missing") from exc
    if not provider_id:
        raise PackageShapeError("distribution provider identity is missing")
    return os.path.normcase(os.path.realpath(os.path.abspath(provider_id)))


@dataclass(frozen=True, order=True)
class DistributionProvider:
    """One exact installed dist-info provider."""

    name: str
    version: str
    provider_id: str
    requires_dist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        canonical_name = _canonical_distribution_name(
            self.name,
            field="distribution provider",
        )
        object.__setattr__(self, "name", canonical_name)
        object.__setattr__(
            self,
            "version",
            _normalized_version(self.version, field=f"{canonical_name} version"),
        )
        object.__setattr__(self, "provider_id", _normalized_provider_id(self.provider_id))
        object.__setattr__(self, "requires_dist", tuple(self.requires_dist))


@dataclass(frozen=True)
class CapturedPackageShape:
    """Immutable package evidence captured before any mutation can start."""

    core_provider: DistributionProvider
    launcher: ServiceLauncher
    release_family: ReleaseFamily
    memory_providers: tuple[DistributionProvider, ...]
    transition_memory_version: str | None
    residual_memory: bool

    def __post_init__(self) -> None:
        transition_memory_version = self.transition_memory_version
        if transition_memory_version is not None:
            transition_memory_version = _normalized_version(
                transition_memory_version,
                field="transition Memory requirement",
            )
        object.__setattr__(self, "memory_providers", tuple(self.memory_providers))
        object.__setattr__(self, "transition_memory_version", transition_memory_version)
        _validate_canonical_package_shape(self, error_type=PackageShapeError)

    @property
    def core_version(self) -> str:
        return self.core_provider.version

    @property
    def core_distribution(self) -> str:
        return self.core_provider.name

    @property
    def memory_present(self) -> bool:
        return bool(self.memory_providers)

    @property
    def memory_version(self) -> str | None:
        return self.memory_providers[0].version if self.memory_providers else None

    @property
    def memory_provider_cardinality(self) -> int:
        return len(self.memory_providers)

    # Compatibility projections for the pre-Gate-3 restart supervisor. New code
    # consumes the exact fields above and never reconstructs package presence from
    # a separate boolean/version pair.
    @property
    def version(self) -> str:
        return self.core_version

    @property
    def package(self) -> str:
        return self.core_distribution

    @property
    def memory_package(self) -> bool:
        return self.memory_present


@dataclass(frozen=True, order=True)
class ExactRequirement:
    distribution: str
    version: str

    def __post_init__(self) -> None:
        canonical_name = _canonical_distribution_name(
            self.distribution,
            field="rollback requirement distribution",
        )
        object.__setattr__(self, "distribution", canonical_name)
        object.__setattr__(
            self,
            "version",
            _normalized_version(self.version, field=f"{canonical_name} rollback version"),
        )

    @property
    def specifier(self) -> str:
        return f"{self.distribution}=={self.version}"


@dataclass(frozen=True, order=True)
class StagedArtifact:
    distribution: str
    version: str
    path: Path
    sha256: str
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class PackageShapeVerification:
    core_distribution: str
    core_version: str
    absent_core_distributions: tuple[str, ...]
    memory_provider_cardinality: int
    memory_version: str | None
    residual_memory: bool


@dataclass(frozen=True, init=False)
class ResolvedRollbackPlan:
    """A rollback whose complete exact closure already exists in staging."""

    captured: CapturedPackageShape
    requirements: tuple[ExactRequirement, ...]
    artifacts: tuple[StagedArtifact, ...]
    staging_dir: Path
    uninstall_distributions: tuple[str, ...]
    verification: PackageShapeVerification

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("ResolvedRollbackPlan is constructed only by rollback resolution")


@dataclass(frozen=True, order=True)
class DistributionProviderRecord:
    """Filesystem-independent evidence for one captured provider."""

    name: str
    version: str
    provider_id: str
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class CapturedPackageShapeRecord:
    """Non-executable persisted form of a captured package shape."""

    core_provider: DistributionProviderRecord
    launcher: ServiceLauncher
    release_family: ReleaseFamily
    memory_providers: tuple[DistributionProviderRecord, ...]
    transition_memory_version: str | None
    residual_memory: bool

    @property
    def core_version(self) -> str:
        return self.core_provider.version

    @property
    def core_distribution(self) -> str:
        return self.core_provider.name

    @property
    def memory_present(self) -> bool:
        return bool(self.memory_providers)

    @property
    def memory_version(self) -> str | None:
        return self.memory_providers[0].version if self.memory_providers else None

    @property
    def memory_provider_cardinality(self) -> int:
        return len(self.memory_providers)


@dataclass(frozen=True, order=True)
class StagedArtifactRecord:
    """Recorded artifact identity; existence and hashes require live revalidation."""

    distribution: str
    version: str
    path: str
    sha256: str
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedRollbackPlanRecord:
    """Non-executable persisted form of a resolved rollback plan."""

    captured: CapturedPackageShapeRecord
    requirements: tuple[ExactRequirement, ...]
    artifacts: tuple[StagedArtifactRecord, ...]
    staging_dir: str
    uninstall_distributions: tuple[str, ...]
    verification: PackageShapeVerification


def _record_object(
    value: object,
    keys: frozenset[str],
    *,
    field: str,
) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        raise PackageShapeRecordError(f"{field} has unknown or missing fields")
    return value


def _record_list(value: object, *, field: str) -> list[object]:
    if type(value) is not list:
        raise PackageShapeRecordError(f"{field} is not a JSON array")
    return value


def _record_string(value: object, *, field: str) -> str:
    if type(value) is not str or not value:
        raise PackageShapeRecordError(f"{field} is missing")
    return value


def _record_strings(value: object, *, field: str) -> tuple[str, ...]:
    return tuple(
        _record_string(item, field=f"{field} item")
        for item in _record_list(value, field=field)
    )


def _record_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise PackageShapeRecordError(f"{field} is not a boolean")
    return value


def _record_int(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise PackageShapeRecordError(f"{field} is not an integer")
    return value


def _record_version(value: object, *, field: str) -> str:
    raw = _record_string(value, field=field)
    try:
        normalized = _normalized_version(raw, field=field)
    except PackageShapeError as exc:
        raise PackageShapeRecordError(str(exc)) from exc
    if raw != normalized:
        raise PackageShapeRecordError(f"{field} is not canonical")
    return raw


def _record_distribution(value: object, *, field: str) -> str:
    raw = _record_string(value, field=field)
    return _canonical_distribution_name(
        raw,
        field=field,
        error_type=PackageShapeRecordError,
        require_canonical=True,
    )


def _record_schema(envelope: dict[str, object]) -> None:
    version = envelope["schema_version"]
    if type(version) is not int or version != PACKAGE_SHAPE_RECORD_SCHEMA_VERSION:
        raise PackageShapeRecordError("package-shape record schema version is unsupported")


def _provider_to_record(provider: DistributionProvider) -> DistributionProviderRecord:
    return DistributionProviderRecord(
        name=provider.name,
        version=provider.version,
        provider_id=provider.provider_id,
        requires_dist=provider.requires_dist,
    )


def _decode_provider(value: object, *, field: str) -> DistributionProviderRecord:
    payload = _record_object(
        value,
        frozenset({"name", "version", "provider_id", "requires_dist"}),
        field=field,
    )
    return DistributionProviderRecord(
        name=_record_distribution(payload["name"], field=f"{field} name"),
        version=_record_version(payload["version"], field=f"{field} version"),
        provider_id=_record_string(payload["provider_id"], field=f"{field} identity"),
        requires_dist=_record_strings(
            payload["requires_dist"],
            field=f"{field} requirements",
        ),
    )


def _encode_provider(provider: DistributionProviderRecord) -> dict[str, object]:
    return {
        "name": provider.name,
        "version": provider.version,
        "provider_id": provider.provider_id,
        "requires_dist": list(provider.requires_dist),
    }


def _captured_to_record(
    captured: CapturedPackageShape | CapturedPackageShapeRecord,
) -> CapturedPackageShapeRecord:
    if isinstance(captured, CapturedPackageShapeRecord):
        return captured
    if not isinstance(captured, CapturedPackageShape):
        raise PackageShapeRecordError("captured package shape has an unsupported type")
    return CapturedPackageShapeRecord(
        core_provider=_provider_to_record(captured.core_provider),
        launcher=captured.launcher,
        release_family=captured.release_family,
        memory_providers=tuple(
            _provider_to_record(item) for item in captured.memory_providers
        ),
        transition_memory_version=captured.transition_memory_version,
        residual_memory=captured.residual_memory,
    )


def encode_captured_package_shape_record(
    captured: CapturedPackageShape | CapturedPackageShapeRecord,
) -> dict[str, object]:
    """Encode captured evidence into a versioned JSON-safe object."""

    record = _captured_to_record(captured)
    try:
        payload: dict[str, object] = {
            "schema_version": PACKAGE_SHAPE_RECORD_SCHEMA_VERSION,
            "record_type": "captured_package_shape",
            "shape": {
                "core_provider": _encode_provider(record.core_provider),
                "launcher": {
                    "python": record.launcher.python,
                    "main": record.launcher.main,
                },
                "release_family": record.release_family.value,
                "memory_providers": [
                    _encode_provider(provider) for provider in record.memory_providers
                ],
                "transition_memory_version": record.transition_memory_version,
                "residual_memory": record.residual_memory,
            },
        }
    except PackageShapeRecordError:
        raise
    except (AttributeError, TypeError) as exc:
        raise PackageShapeRecordError("captured record DTO is invalid") from exc
    if decode_captured_package_shape_record(payload) != record:
        raise PackageShapeRecordError("captured record DTO is not canonical")
    return payload


def decode_captured_package_shape_record(value: object) -> CapturedPackageShapeRecord:
    """Decode captured evidence without consulting installed package state."""

    envelope = _record_object(
        value,
        frozenset({"schema_version", "record_type", "shape"}),
        field="captured package-shape record",
    )
    _record_schema(envelope)
    if envelope["record_type"] != "captured_package_shape":
        raise PackageShapeRecordError("captured package-shape record type is unsupported")
    shape = _record_object(
        envelope["shape"],
        frozenset(
            {
                "core_provider",
                "launcher",
                "release_family",
                "memory_providers",
                "transition_memory_version",
                "residual_memory",
            }
        ),
        field="captured package shape",
    )
    launcher = _record_object(
        shape["launcher"],
        frozenset({"python", "main"}),
        field="service launcher",
    )
    family_value = _record_string(shape["release_family"], field="release family")
    try:
        family = ReleaseFamily(family_value)
    except ValueError as exc:
        raise PackageShapeRecordError("captured release family is unknown") from exc
    transition = shape["transition_memory_version"]
    if transition is not None:
        transition = _record_version(transition, field="transition Memory version")
    record = CapturedPackageShapeRecord(
        core_provider=_decode_provider(shape["core_provider"], field="core provider"),
        launcher=ServiceLauncher(
            python=_record_string(launcher["python"], field="launcher Python"),
            main=_record_string(launcher["main"], field="launcher main"),
        ),
        release_family=family,
        memory_providers=tuple(
            _decode_provider(item, field="Memory provider")
            for item in _record_list(
                shape["memory_providers"],
                field="Memory providers",
            )
        ),
        transition_memory_version=transition,
        residual_memory=_record_bool(
            shape["residual_memory"],
            field="residual Memory",
        ),
    )
    _validate_canonical_package_shape(
        record,
        error_type=PackageShapeRecordError,
    )
    return record


def _decode_requirement(value: object) -> ExactRequirement:
    payload = _record_object(
        value,
        frozenset({"distribution", "version"}),
        field="rollback requirement",
    )
    return ExactRequirement(
        distribution=_record_distribution(
            payload["distribution"],
            field="requirement distribution",
        ),
        version=_record_version(payload["version"], field="requirement version"),
    )


def _decode_artifact(value: object) -> StagedArtifactRecord:
    payload = _record_object(
        value,
        frozenset({"distribution", "version", "path", "sha256", "requires_dist"}),
        field="staged artifact",
    )
    digest = _record_string(payload["sha256"], field="artifact SHA-256")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise PackageShapeRecordError("artifact SHA-256 is not canonical")
    return StagedArtifactRecord(
        distribution=_record_distribution(
            payload["distribution"],
            field="artifact distribution",
        ),
        version=_record_version(payload["version"], field="artifact version"),
        path=_record_string(payload["path"], field="artifact path"),
        sha256=digest,
        requires_dist=_record_strings(
            payload["requires_dist"],
            field="artifact requirements",
        ),
    )


def _decode_verification(value: object) -> PackageShapeVerification:
    payload = _record_object(
        value,
        frozenset(
            {
                "core_distribution",
                "core_version",
                "absent_core_distributions",
                "memory_provider_cardinality",
                "memory_version",
                "residual_memory",
            }
        ),
        field="package-shape verification",
    )
    memory_version = payload["memory_version"]
    if memory_version is not None:
        memory_version = _record_version(
            memory_version,
            field="verified Memory version",
        )
    return PackageShapeVerification(
        core_distribution=_record_distribution(
            payload["core_distribution"],
            field="verified core distribution",
        ),
        core_version=_record_version(
            payload["core_version"],
            field="verified core version",
        ),
        absent_core_distributions=tuple(
            _record_distribution(item, field="absent core distribution")
            for item in _record_list(
                payload["absent_core_distributions"],
                field="absent core distributions",
            )
        ),
        memory_provider_cardinality=_record_int(
            payload["memory_provider_cardinality"],
            field="Memory provider cardinality",
        ),
        memory_version=memory_version,
        residual_memory=_record_bool(
            payload["residual_memory"],
            field="verified residual Memory",
        ),
    )


def _plan_to_record(
    plan: ResolvedRollbackPlan | ResolvedRollbackPlanRecord,
) -> ResolvedRollbackPlanRecord:
    if isinstance(plan, ResolvedRollbackPlanRecord):
        return plan
    if not isinstance(plan, ResolvedRollbackPlan):
        raise PackageShapeRecordError("resolved rollback plan has an unsupported type")
    return ResolvedRollbackPlanRecord(
        captured=_captured_to_record(plan.captured),
        requirements=plan.requirements,
        artifacts=tuple(
            StagedArtifactRecord(
                distribution=item.distribution,
                version=item.version,
                path=str(item.path),
                sha256=item.sha256,
                requires_dist=item.requires_dist,
            )
            for item in plan.artifacts
        ),
        staging_dir=str(plan.staging_dir),
        uninstall_distributions=plan.uninstall_distributions,
        verification=plan.verification,
    )


def _validate_plan_record(record: ResolvedRollbackPlanRecord) -> None:
    _validate_canonical_package_shape(
        record.captured,
        error_type=PackageShapeRecordError,
    )
    if not record.requirements or not record.artifacts:
        raise PackageShapeRecordError("resolved rollback record has an empty closure")
    try:
        expected_requirements = _rollback_requirements(record.captured)
    except PackageShapeError as exc:
        raise PackageShapeRecordError(str(exc)) from exc
    if record.requirements != expected_requirements:
        raise PackageShapeRecordError(
            "resolved requirements do not match the captured rollback target"
        )
    try:
        _verify_staged_shape(record.captured, record.artifacts)
    except PackageShapeError as exc:
        raise PackageShapeRecordError(str(exc)) from exc
    if record.uninstall_distributions != _rollback_uninstall_distributions():
        raise PackageShapeRecordError("resolved rollback record has an unknown uninstall set")
    if record.verification != _package_shape_verification(record.captured):
        raise PackageShapeRecordError("resolved rollback verification is inconsistent")


def encode_resolved_rollback_plan_record(
    plan: ResolvedRollbackPlan | ResolvedRollbackPlanRecord,
) -> dict[str, object]:
    """Encode a plan as data that cannot itself authorize execution."""

    record = _plan_to_record(plan)
    try:
        payload: dict[str, object] = {
            "schema_version": PACKAGE_SHAPE_RECORD_SCHEMA_VERSION,
            "record_type": "resolved_rollback_plan",
            "plan": {
                "captured": encode_captured_package_shape_record(record.captured),
                "requirements": [
                    {
                        "distribution": item.distribution,
                        "version": item.version,
                    }
                    for item in record.requirements
                ],
                "artifacts": [
                    {
                        "distribution": item.distribution,
                        "version": item.version,
                        "path": item.path,
                        "sha256": item.sha256,
                        "requires_dist": list(item.requires_dist),
                    }
                    for item in record.artifacts
                ],
                "staging_dir": record.staging_dir,
                "uninstall_distributions": list(record.uninstall_distributions),
                "verification": {
                    "core_distribution": record.verification.core_distribution,
                    "core_version": record.verification.core_version,
                    "absent_core_distributions": list(
                        record.verification.absent_core_distributions
                    ),
                    "memory_provider_cardinality": (
                        record.verification.memory_provider_cardinality
                    ),
                    "memory_version": record.verification.memory_version,
                    "residual_memory": record.verification.residual_memory,
                },
            },
        }
    except PackageShapeRecordError:
        raise
    except (AttributeError, TypeError) as exc:
        raise PackageShapeRecordError("resolved record DTO is invalid") from exc
    if decode_resolved_rollback_plan_record(payload) != record:
        raise PackageShapeRecordError("resolved record DTO is not canonical")
    return payload


def decode_resolved_rollback_plan_record(value: object) -> ResolvedRollbackPlanRecord:
    """Decode plan data without constructing an executable rollback plan."""

    envelope = _record_object(
        value,
        frozenset({"schema_version", "record_type", "plan"}),
        field="resolved rollback record",
    )
    _record_schema(envelope)
    if envelope["record_type"] != "resolved_rollback_plan":
        raise PackageShapeRecordError("resolved rollback record type is unsupported")
    plan = _record_object(
        envelope["plan"],
        frozenset(
            {
                "captured",
                "requirements",
                "artifacts",
                "staging_dir",
                "uninstall_distributions",
                "verification",
            }
        ),
        field="resolved rollback plan",
    )
    record = ResolvedRollbackPlanRecord(
        captured=decode_captured_package_shape_record(plan["captured"]),
        requirements=tuple(
            _decode_requirement(item)
            for item in _record_list(
                plan["requirements"],
                field="rollback requirements",
            )
        ),
        artifacts=tuple(
            _decode_artifact(item)
            for item in _record_list(plan["artifacts"], field="staged artifacts")
        ),
        staging_dir=_record_string(
            plan["staging_dir"],
            field="staging directory",
        ),
        uninstall_distributions=tuple(
            _record_distribution(item, field="uninstall distribution")
            for item in _record_list(
                plan["uninstall_distributions"],
                field="uninstall distributions",
            )
        ),
        verification=_decode_verification(plan["verification"]),
    )
    _validate_plan_record(record)
    return record


def _parsed_memory_requirements(
    provider: DistributionProvider | DistributionProviderRecord,
) -> tuple[Requirement, ...]:
    parsed: list[Requirement] = []
    for value in provider.requires_dist:
        try:
            requirement = Requirement(value)
        except (InvalidRequirement, TypeError) as exc:
            raise PackageShapeError("core distribution contains unreadable dependency metadata") from exc
        if (
            _canonical_distribution_name(
                requirement.name,
                field="core requirement distribution",
            )
            == MEMORY_PACKAGE_NAME
        ):
            parsed.append(requirement)
    return tuple(parsed)


def _release_family(
    provider: DistributionProvider | DistributionProviderRecord,
) -> tuple[ReleaseFamily, str | None]:
    memory_requirements = _parsed_memory_requirements(provider)
    hard: list[Requirement] = []
    for requirement in memory_requirements:
        if requirement.marker is None:
            hard.append(requirement)
            continue
        if str(requirement.marker) != 'extra == "memory"':
            raise PackageShapeError("conditional Memory dependency does not name the optional release family")

    version = Version(provider.version)
    if version < MEMORY_SPLIT_MIN_VERSION:
        if memory_requirements:
            raise PackageShapeError("pre-split core declares a split Memory dependency")
        return ReleaseFamily.PRE_SPLIT, None

    if not memory_requirements:
        raise PackageShapeError("split core does not declare a known Memory release family")
    if not hard:
        return ReleaseFamily.OPTIONAL_SPLIT, None
    if len(hard) != 1 or len(memory_requirements) != 1:
        raise PackageShapeError("transition core must declare one exact Memory dependency")

    specifiers = list(hard[0].specifier)
    if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
        raise PackageShapeError("transition core Memory dependency is not an exact pin")
    required_version = _normalized_version(
        specifiers[0].version,
        field="transition Memory requirement",
    )
    if required_version != provider.version:
        raise PackageShapeError("transition core does not require its matching Memory release")
    return ReleaseFamily.TRANSITION, required_version


def _validate_canonical_package_shape(
    shape: CapturedPackageShape | CapturedPackageShapeRecord,
    *,
    error_type: type[PackageShapeError],
) -> None:
    provider_type = (
        DistributionProviderRecord
        if isinstance(shape, CapturedPackageShapeRecord)
        else DistributionProvider
    )
    if not isinstance(shape.release_family, ReleaseFamily):
        raise error_type("captured release family is unknown")
    if not isinstance(shape.core_provider, provider_type):
        raise error_type("captured core provider is invalid")
    if shape.core_provider.name not in CORE_DISTRIBUTION_NAMES:
        raise error_type("captured core provider is not an Avibe distribution")
    if not isinstance(shape.launcher, ServiceLauncher):
        raise error_type("captured service launcher is invalid")
    if type(shape.memory_providers) is not tuple or any(
        not isinstance(provider, provider_type) for provider in shape.memory_providers
    ):
        raise error_type("captured Memory providers are invalid")
    if len(shape.memory_providers) > 1:
        raise error_type("multiple canonical avibe-memory providers are not representable")
    if any(provider.name != MEMORY_PACKAGE_NAME for provider in shape.memory_providers):
        raise error_type("captured Memory provider has the wrong canonical name")
    if type(shape.residual_memory) is not bool:
        raise error_type("captured residual Memory marker is invalid")

    try:
        derived_family, derived_transition = _release_family(shape.core_provider)
    except PackageShapeError as exc:
        raise error_type(str(exc)) from exc
    if shape.release_family is not derived_family:
        raise error_type("captured release family disagrees with core metadata")
    if shape.transition_memory_version != derived_transition:
        raise error_type("captured transition pin disagrees with core metadata")
    expected_residual = derived_family is ReleaseFamily.PRE_SPLIT and shape.memory_present
    if shape.residual_memory is not expected_residual:
        raise error_type("residual Memory marker disagrees with the derived family")


def capture_package_shape(
    *,
    core_version: str,
    launcher: ServiceLauncher,
    providers: Iterable[DistributionProvider],
) -> CapturedPackageShape:
    """Validate an exact provider inventory into one immutable captured shape."""

    normalized_core_version = _normalized_version(core_version, field="running core version")
    observed = tuple(
        provider
        for provider in providers
        if provider.name in CORE_DISTRIBUTION_NAMES or provider.name == MEMORY_PACKAGE_NAME
    )
    by_identity: dict[str, DistributionProvider] = {}
    for provider in observed:
        existing = by_identity.get(provider.provider_id)
        if existing is not None and existing != provider:
            raise PackageShapeError(
                "canonical distribution provider identity has conflicting metadata"
            )
        by_identity[provider.provider_id] = provider
    relevant = tuple(by_identity.values())

    by_name: dict[str, list[DistributionProvider]] = {}
    for provider in relevant:
        by_name.setdefault(provider.name, []).append(provider)
    duplicate = next((name for name, values in by_name.items() if len(values) > 1), None)
    if duplicate is not None:
        raise DuplicateDistributionProviderError(
            f"multiple canonical {duplicate} dist-info providers are installed"
        )

    installed_core_providers = tuple(
        provider for provider in relevant if provider.name in CORE_DISTRIBUTION_NAMES
    )
    if not installed_core_providers:
        raise PackageShapeError("running core has no canonical distribution provider")

    # A rename or rollback can leave one provider for each published core name.
    # The running version still has to identify exactly one of those providers.
    core_candidates = tuple(
        provider
        for provider in installed_core_providers
        if provider.name in CORE_DISTRIBUTION_NAMES and provider.version == normalized_core_version
    )
    if len(core_candidates) != 1:
        raise PackageShapeError("running core does not have exactly one canonical distribution provider")
    memory_providers = tuple(
        provider for provider in relevant if provider.name == MEMORY_PACKAGE_NAME
    )
    family, transition_memory_version = _release_family(core_candidates[0])
    return CapturedPackageShape(
        core_provider=core_candidates[0],
        launcher=launcher,
        release_family=family,
        memory_providers=memory_providers,
        transition_memory_version=transition_memory_version,
        residual_memory=family is ReleaseFamily.PRE_SPLIT and bool(memory_providers),
    )


def _distribution_provider_id(distribution: Any) -> str:
    metadata_path = getattr(distribution, "_path", None)
    if metadata_path is not None:
        return os.fspath(metadata_path)
    files = distribution.files
    if files is not None:
        for entry in files:
            if str(entry).endswith((".dist-info/METADATA", ".egg-info/PKG-INFO")):
                return os.fspath(distribution.locate_file(entry))
    raise PackageShapeError("installed distribution provider identity is unreadable")


def inspect_installed_distribution_providers(
    distributions: Iterable[Any] | None = None,
) -> tuple[DistributionProvider, ...]:
    """Read relevant installed metadata without importing optional implementation."""

    if distributions is None:
        from importlib.metadata import distributions as installed_distributions

        distributions = installed_distributions()

    providers: list[DistributionProvider] = []
    try:
        for distribution in distributions:
            name = distribution.metadata["Name"]
            if not isinstance(name, str) or not name.strip():
                raise PackageShapeError("installed distribution name is unreadable")
            canonical_name = _canonical_distribution_name(
                name,
                field="installed distribution",
            )
            if canonical_name not in CORE_DISTRIBUTION_NAMES and canonical_name != MEMORY_PACKAGE_NAME:
                continue
            providers.append(
                DistributionProvider(
                    name=canonical_name,
                    version=distribution.version,
                    provider_id=_distribution_provider_id(distribution),
                    requires_dist=tuple(distribution.requires or ()),
                )
            )
    except PackageShapeError:
        raise
    except Exception as exc:
        raise PackageShapeError("installed distribution metadata is unreadable") from exc
    return tuple(providers)


def capture_installed_package_shape(
    *,
    core_version: str,
    launcher: ServiceLauncher,
    distributions: Iterable[Any] | None = None,
) -> CapturedPackageShape:
    return capture_package_shape(
        core_version=core_version,
        launcher=launcher,
        providers=inspect_installed_distribution_providers(distributions),
    )


def _rollback_requirements(
    captured: CapturedPackageShape | CapturedPackageShapeRecord,
) -> tuple[ExactRequirement, ...]:
    if captured.release_family is ReleaseFamily.TRANSITION:
        if not captured.memory_present:
            raise RollbackResolutionError(
                "transition package shape is missing its exact Memory distribution"
            )
        if captured.memory_version != captured.transition_memory_version:
            raise RollbackResolutionError(
                "transition package shape does not match its exact Memory dependency"
            )

    requirements = [
        ExactRequirement(captured.core_distribution, captured.core_version),
    ]
    if captured.memory_present:
        # Presence implies a normalized exact version by construction. Keeping the
        # pin independent is essential: an optional extra cannot express a captured
        # optional-era mismatch or a bundled core's residual split distribution.
        assert captured.memory_version is not None
        requirements.append(ExactRequirement(MEMORY_PACKAGE_NAME, captured.memory_version))
    return tuple(requirements)


def _wheel_provider(path: Path) -> DistributionProvider:
    try:
        filename_name, filename_version, _, _ = parse_wheel_filename(path.name)
    except InvalidWheelFilename as exc:
        raise RollbackResolutionError("staging contains a non-wheel package artifact") from exc
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise RollbackResolutionError("staged wheel metadata is missing or ambiguous")
            metadata = email.parser.Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise RollbackResolutionError("staged wheel metadata is unreadable") from exc

    provider = DistributionProvider(
        name=metadata.get("Name"),
        version=metadata.get("Version"),
        provider_id=str(path),
        requires_dist=tuple(metadata.get_all("Requires-Dist", [])),
    )
    filename_distribution = _canonical_distribution_name(
        str(filename_name),
        field="wheel filename distribution",
        error_type=RollbackResolutionError,
    )
    if provider.name != filename_distribution or provider.version != str(filename_version):
        raise RollbackResolutionError("staged wheel filename and metadata disagree")
    return provider


def _staged_artifacts(staging_dir: Path) -> tuple[StagedArtifact, ...]:
    artifacts: list[StagedArtifact] = []
    for path in sorted(staging_dir.iterdir()):
        if not path.is_file():
            raise RollbackResolutionError("staging contains a non-artifact entry")
        provider = _wheel_provider(path)
        artifacts.append(
            StagedArtifact(
                distribution=provider.name,
                version=provider.version,
                path=path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                requires_dist=provider.requires_dist,
            )
        )
    if not artifacts:
        raise RollbackResolutionError("rollback resolution produced no staged artifacts")
    return tuple(artifacts)


def _verify_staged_shape(
    captured: CapturedPackageShape | CapturedPackageShapeRecord,
    artifacts: tuple[StagedArtifact, ...] | tuple[StagedArtifactRecord, ...],
) -> None:
    by_distribution: dict[
        str,
        list[StagedArtifact | StagedArtifactRecord],
    ] = {}
    for artifact in artifacts:
        by_distribution.setdefault(artifact.distribution, []).append(artifact)
    if any(len(values) != 1 for values in by_distribution.values()):
        raise RollbackResolutionError("staging contains duplicate distribution artifacts")

    core_artifacts = tuple(
        artifact
        for artifact in artifacts
        if artifact.distribution in CORE_DISTRIBUTION_NAMES
    )
    if len(core_artifacts) != 1 or (
        core_artifacts[0].distribution,
        core_artifacts[0].version,
    ) != (captured.core_distribution, captured.core_version):
        raise RollbackResolutionError("staging does not contain the exact captured core shape")

    memory_artifacts = tuple(
        artifact
        for artifact in artifacts
        if artifact.distribution == MEMORY_PACKAGE_NAME
    )
    staged_memory_version = memory_artifacts[0].version if len(memory_artifacts) == 1 else None
    if (
        len(memory_artifacts) != captured.memory_provider_cardinality
        or staged_memory_version != captured.memory_version
    ):
        raise RollbackResolutionError("staging does not contain the exact captured Memory shape")


def _rollback_uninstall_distributions() -> tuple[str, ...]:
    return tuple(sorted((*CORE_DISTRIBUTION_NAMES, MEMORY_PACKAGE_NAME)))


def _package_shape_verification(
    captured: CapturedPackageShape | CapturedPackageShapeRecord,
) -> PackageShapeVerification:
    return PackageShapeVerification(
        core_distribution=captured.core_distribution,
        core_version=captured.core_version,
        absent_core_distributions=tuple(
            sorted(CORE_DISTRIBUTION_NAMES - {captured.core_distribution})
        ),
        memory_provider_cardinality=captured.memory_provider_cardinality,
        memory_version=captured.memory_version,
        residual_memory=captured.residual_memory,
    )


def _construct_resolved_plan(
    *,
    captured: CapturedPackageShape,
    requirements: tuple[ExactRequirement, ...],
    artifacts: tuple[StagedArtifact, ...],
    staging_dir: Path,
) -> ResolvedRollbackPlan:
    plan = object.__new__(ResolvedRollbackPlan)
    object.__setattr__(plan, "captured", captured)
    object.__setattr__(plan, "requirements", requirements)
    object.__setattr__(plan, "artifacts", artifacts)
    object.__setattr__(plan, "staging_dir", staging_dir)
    object.__setattr__(
        plan,
        "uninstall_distributions",
        _rollback_uninstall_distributions(),
    )
    object.__setattr__(
        plan,
        "verification",
        _package_shape_verification(captured),
    )
    return plan


def resolve_rollback_plan(
    captured: CapturedPackageShape,
    *,
    wheelhouse: Path,
    staging_dir: Path,
    resolver_python: str | None = None,
) -> ResolvedRollbackPlan:
    """Resolve exact requirements from one local wheelhouse without installing."""

    requirements = _rollback_requirements(captured)
    wheelhouse = Path(wheelhouse).resolve()
    staging_dir = Path(staging_dir).resolve()
    if not wheelhouse.is_dir():
        raise RollbackResolutionError("rollback wheelhouse does not exist")
    if staging_dir.exists():
        raise RollbackResolutionError("rollback staging destination already exists")
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{staging_dir.name}-", dir=staging_dir.parent))
    command_prefix = [
        resolver_python or sys.executable,
        "-m",
        "pip",
        "download",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--only-binary=:all:",
        "--dest",
        str(temporary),
    ]
    resolver_requests = ((False, requirements),)
    if captured.residual_memory:
        # Residual Memory metadata points at the replacement avibe-os family.
        # Resolve the legacy core closure normally, then stage that exact
        # residual wheel without following its forward-core dependency.
        core_requirements = tuple(
            requirement
            for requirement in requirements
            if requirement.distribution != MEMORY_PACKAGE_NAME
        )
        memory_requirements = tuple(
            requirement
            for requirement in requirements
            if requirement.distribution == MEMORY_PACKAGE_NAME
        )
        resolver_requests = (
            (False, core_requirements),
            (True, memory_requirements),
        )
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PIP_") and key != "PYTHONPATH"
        },
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
        "PYTHONNOUSERSITE": "1",
    }
    moved_to_staging = False
    resolved = False
    try:
        for without_dependencies, requested in resolver_requests:
            command = [
                *command_prefix,
                *(["--no-deps"] if without_dependencies else []),
                *(requirement.specifier for requirement in requested),
            ]
            result = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            if result.returncode != 0:
                raise RollbackResolutionError(
                    "exact rollback requirements did not resolve from the local wheelhouse"
                )
        temporary_artifacts = _staged_artifacts(temporary)
        _verify_staged_shape(captured, temporary_artifacts)
        staged_versions = {
            (artifact.distribution, artifact.version) for artifact in temporary_artifacts
        }
        missing = [
            requirement.specifier
            for requirement in requirements
            if (requirement.distribution, requirement.version) not in staged_versions
        ]
        if missing:
            raise RollbackResolutionError("resolved staging is missing an exact rollback artifact")

        staged_core = next(
            artifact
            for artifact in temporary_artifacts
            if artifact.distribution == captured.core_distribution
            and artifact.version == captured.core_version
        )
        staged_family, staged_transition_version = _release_family(
            DistributionProvider(
                name=staged_core.distribution,
                version=staged_core.version,
                provider_id=str(staged_core.path),
                requires_dist=staged_core.requires_dist,
            )
        )
        if (
            staged_family is not captured.release_family
            or staged_transition_version != captured.transition_memory_version
        ):
            raise RollbackResolutionError(
                "staged core artifact does not match the captured release family"
            )

        if staging_dir.exists():
            raise RollbackResolutionError("rollback staging destination appeared during resolution")
        os.replace(temporary, staging_dir)
        moved_to_staging = True
        artifacts = _staged_artifacts(staging_dir)
        plan = _construct_resolved_plan(
            captured=captured,
            requirements=requirements,
            artifacts=artifacts,
            staging_dir=staging_dir,
        )
        resolved = True
        return plan
    except (OSError, subprocess.SubprocessError) as exc:
        raise RollbackResolutionError("rollback artifact staging failed") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if moved_to_staging and not resolved and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


__all__ = [
    "CapturedPackageShape",
    "CapturedPackageShapeRecord",
    "DistributionProvider",
    "DistributionProviderRecord",
    "DuplicateDistributionProviderError",
    "ExactRequirement",
    "PACKAGE_SHAPE_RECORD_SCHEMA_VERSION",
    "PackageShapeError",
    "PackageShapeRecordError",
    "PackageShapeVerification",
    "ReleaseFamily",
    "ResolvedRollbackPlan",
    "ResolvedRollbackPlanRecord",
    "RollbackResolutionError",
    "StagedArtifact",
    "StagedArtifactRecord",
    "capture_installed_package_shape",
    "capture_package_shape",
    "decode_captured_package_shape_record",
    "decode_resolved_rollback_plan_record",
    "encode_captured_package_shape_record",
    "encode_resolved_rollback_plan_record",
    "inspect_installed_distribution_providers",
    "resolve_rollback_plan",
]
