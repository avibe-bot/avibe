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
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

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
        try:
            _captured_to_record(self)
        except PackageShapeRecordError as exc:
            raise PackageShapeError(str(exc)) from exc

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

    def __post_init__(self) -> None:
        _validate_record_fields(self)
        _require_record_distribution(self.name, field="distribution provider")
        _require_record_version(self.version, field=f"{self.name} version")
        if self.provider_id != _normalized_provider_id(self.provider_id):
            raise PackageShapeRecordError("distribution provider identity is not canonical")


@dataclass(frozen=True)
class _ProviderInventoryRecord:
    providers: tuple[DistributionProviderRecord, ...]

    def __post_init__(self) -> None:
        _validate_record_fields(self)
        by_identity: dict[str, DistributionProviderRecord] = {}
        for provider in self.providers:
            existing = by_identity.get(provider.provider_id)
            if existing is not None and existing != provider:
                raise PackageShapeRecordError("canonical distribution provider identity has conflicting metadata")
            by_identity[provider.provider_id] = provider


@dataclass(frozen=True)
class _ServiceLauncherRecord:
    python: str
    main: str

    def __post_init__(self) -> None:
        _validate_record_fields(self)


@dataclass(frozen=True)
class CapturedPackageShapeRecord:
    """Non-executable persisted form of a captured package shape."""

    core_provider: DistributionProviderRecord
    launcher: _ServiceLauncherRecord
    release_family: ReleaseFamily
    memory_providers: tuple[DistributionProviderRecord, ...]
    transition_memory_version: str | None
    residual_memory: bool

    def __post_init__(self) -> None:
        _validate_record_fields(self)
        if self.core_provider.name not in CORE_DISTRIBUTION_NAMES:
            raise PackageShapeRecordError("captured core provider is not an Avibe distribution")
        if len(self.memory_providers) > 1:
            raise PackageShapeRecordError("multiple canonical avibe-memory providers are not representable")
        if any(provider.name != MEMORY_PACKAGE_NAME for provider in self.memory_providers):
            raise PackageShapeRecordError("captured Memory provider has the wrong canonical name")
        if self.transition_memory_version is not None:
            _require_record_version(
                self.transition_memory_version,
                field="transition Memory requirement",
            )

        _ProviderInventoryRecord(providers=(self.core_provider, *self.memory_providers))

        try:
            derived_family, derived_transition = _release_family(self.core_provider)
        except PackageShapeError as exc:
            raise PackageShapeRecordError(str(exc)) from exc
        if self.release_family is not derived_family:
            raise PackageShapeRecordError("captured release family disagrees with core metadata")
        if self.transition_memory_version != derived_transition:
            raise PackageShapeRecordError("captured transition pin disagrees with core metadata")
        expected_residual = derived_family is ReleaseFamily.PRE_SPLIT and self.memory_present
        if self.residual_memory is not expected_residual:
            raise PackageShapeRecordError("residual Memory marker disagrees with the derived family")

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

    def __post_init__(self) -> None:
        _validate_record_fields(self)
        _require_record_distribution(self.distribution, field="artifact distribution")
        _require_record_version(self.version, field=f"{self.distribution} version")
        if len(self.sha256) != 64 or any(character not in "0123456789abcdef" for character in self.sha256):
            raise PackageShapeRecordError("artifact SHA-256 is not canonical")


@dataclass(frozen=True)
class ResolvedRollbackPlanRecord:
    """Non-executable persisted form of a resolved rollback plan."""

    captured: CapturedPackageShapeRecord
    requirements: tuple[ExactRequirement, ...]
    artifacts: tuple[StagedArtifactRecord, ...]
    staging_dir: str
    uninstall_distributions: tuple[str, ...]
    verification: PackageShapeVerification

    def __post_init__(self) -> None:
        _validate_record_fields(self)
        staging_dir = _require_record_absolute_path(
            self.staging_dir,
            field="staging directory",
        )
        if not self.requirements or not self.artifacts:
            raise PackageShapeRecordError("resolved rollback record has an empty closure")

        try:
            expected_requirements = _rollback_requirements(self.captured)
        except PackageShapeError as exc:
            raise PackageShapeRecordError(str(exc)) from exc
        if self.requirements != expected_requirements:
            raise PackageShapeRecordError("resolved requirements do not match the captured rollback target")

        by_distribution: dict[str, StagedArtifactRecord] = {}
        artifact_paths: set[str] = set()
        for artifact in self.artifacts:
            if artifact.distribution in by_distribution:
                raise PackageShapeRecordError("staging contains duplicate distribution artifacts")
            by_distribution[artifact.distribution] = artifact
            artifact_path = _require_record_absolute_path(
                artifact.path,
                field="artifact path",
            )
            if artifact_path.parent != staging_dir:
                raise PackageShapeRecordError("artifact path is not a direct child of the staging directory")
            if artifact.path in artifact_paths:
                raise PackageShapeRecordError("staging contains duplicate artifact paths")
            artifact_paths.add(artifact.path)

        core_artifacts = tuple(
            artifact for artifact in self.artifacts if artifact.distribution in CORE_DISTRIBUTION_NAMES
        )
        if len(core_artifacts) != 1 or (
            core_artifacts[0].distribution,
            core_artifacts[0].version,
        ) != (self.captured.core_distribution, self.captured.core_version):
            raise PackageShapeRecordError("staging does not contain the exact captured core shape")
        memory_artifacts = tuple(
            artifact for artifact in self.artifacts if artifact.distribution == MEMORY_PACKAGE_NAME
        )
        staged_memory_version = memory_artifacts[0].version if len(memory_artifacts) == 1 else None
        if (
            len(memory_artifacts) != self.captured.memory_provider_cardinality
            or staged_memory_version != self.captured.memory_version
        ):
            raise PackageShapeRecordError("staging does not contain the exact captured Memory shape")
        missing_requirements = {
            (requirement.distribution, requirement.version) for requirement in self.requirements
        } - {(artifact.distribution, artifact.version) for artifact in self.artifacts}
        if missing_requirements:
            raise PackageShapeRecordError("resolved staging is missing an exact rollback artifact")

        staged_core = core_artifacts[0]
        try:
            staged_family, staged_transition = _release_family(
                DistributionProviderRecord(
                    name=staged_core.distribution,
                    version=staged_core.version,
                    provider_id=staged_core.path,
                    requires_dist=staged_core.requires_dist,
                )
            )
        except PackageShapeError as exc:
            raise PackageShapeRecordError(str(exc)) from exc
        if (
            staged_family is not self.captured.release_family
            or staged_transition != self.captured.transition_memory_version
        ):
            raise PackageShapeRecordError("staged core artifact does not match the captured release family")
        if self.uninstall_distributions != _rollback_uninstall_distributions():
            raise PackageShapeRecordError("resolved rollback record has an unknown uninstall set")
        if self.verification != _package_shape_verification(self.captured):
            raise PackageShapeRecordError("resolved rollback verification is inconsistent")


_RECORD_DTO_TYPES = (
    DistributionProviderRecord,
    _ProviderInventoryRecord,
    _ServiceLauncherRecord,
    CapturedPackageShapeRecord,
    ExactRequirement,
    StagedArtifactRecord,
    PackageShapeVerification,
    ResolvedRollbackPlanRecord,
)
_RECORD_FIELD_SCHEMAS = {
    dto_type: tuple((field.name, get_type_hints(dto_type)[field.name]) for field in fields(dto_type))
    for dto_type in _RECORD_DTO_TYPES
}


def _validate_declared_record_value(
    value: object,
    annotation: object,
    *,
    field: str,
) -> None:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        options = get_args(annotation)
        if value is None and type(None) in options:
            return
        candidates = tuple(option for option in options if option is not type(None))
        if len(candidates) != 1:
            raise PackageShapeRecordError(f"{field} has an unsupported field schema")
        _validate_declared_record_value(value, candidates[0], field=field)
        return
    if origin is tuple:
        if type(value) is not tuple:
            raise PackageShapeRecordError(f"{field} is not an immutable sequence")
        item_type, marker = get_args(annotation)
        if marker is not Ellipsis:
            raise PackageShapeRecordError(f"{field} has an unsupported field schema")
        for item in value:
            _validate_declared_record_value(item, item_type, field=f"{field} item")
        return
    if annotation in _RECORD_FIELD_SCHEMAS:
        if type(value) is not annotation:
            raise PackageShapeRecordError(f"{field} has an invalid DTO type")
        _validate_record_fields(value)
        return
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is not annotation:
            raise PackageShapeRecordError(f"{field} has an invalid enum value")
        return
    if type(value) is not annotation:
        raise PackageShapeRecordError(f"{field} has an invalid primitive type")
    if annotation is str and not value:
        raise PackageShapeRecordError(f"{field} is missing")


def _validate_record_fields(value: object) -> None:
    schema = _RECORD_FIELD_SCHEMAS.get(type(value))
    if schema is None:
        raise PackageShapeRecordError("record DTO type is unsupported")
    for name, annotation in schema:
        _validate_declared_record_value(
            getattr(value, name),
            annotation,
            field=f"{type(value).__name__}.{name}",
        )


def _require_record_version(value: str, *, field: str) -> None:
    try:
        normalized = _normalized_version(value, field=field)
    except PackageShapeError as exc:
        raise PackageShapeRecordError(str(exc)) from exc
    if value != normalized:
        raise PackageShapeRecordError(f"{field} is not canonical")


def _require_record_distribution(value: str, *, field: str) -> None:
    _canonical_distribution_name(
        value,
        field=field,
        error_type=PackageShapeRecordError,
        require_canonical=True,
    )


def _require_record_absolute_path(value: str, *, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise PackageShapeRecordError(f"{field} is not a canonical absolute path")
    return path


def _encode_declared_record_value(value: object, annotation: object) -> object:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        if value is None:
            return None
        return _encode_declared_record_value(
            value,
            next(option for option in get_args(annotation) if option is not type(None)),
        )
    if origin is tuple:
        item_type = get_args(annotation)[0]
        return [_encode_declared_record_value(item, item_type) for item in value]
    if annotation in _RECORD_FIELD_SCHEMAS:
        return _encode_record_dto(value)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return value.value
    return value


def _encode_record_dto(value: object) -> dict[str, object]:
    _validate_record_fields(value)
    schema = _RECORD_FIELD_SCHEMAS[type(value)]
    try:
        reconstructed = type(value)(**{name: getattr(value, name) for name, _ in schema})
    except PackageShapeError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise PackageShapeRecordError("record DTO is invalid") from exc
    if reconstructed != value:
        raise PackageShapeRecordError("record DTO is not canonical")
    return {name: _encode_declared_record_value(getattr(value, name), annotation) for name, annotation in schema}


def _decode_declared_record_value(
    value: object,
    annotation: object,
    *,
    field: str,
) -> object:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        options = get_args(annotation)
        if value is None and type(None) in options:
            return None
        return _decode_declared_record_value(
            value,
            next(option for option in options if option is not type(None)),
            field=field,
        )
    if origin is tuple:
        if type(value) is not list:
            raise PackageShapeRecordError(f"{field} is not a JSON array")
        item_type = get_args(annotation)[0]
        return tuple(_decode_declared_record_value(item, item_type, field=f"{field} item") for item in value)
    if annotation in _RECORD_FIELD_SCHEMAS:
        return _decode_record_dto(value, annotation, field=field)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is not str:
            raise PackageShapeRecordError(f"{field} has an invalid enum value")
        try:
            return annotation(value)
        except ValueError as exc:
            raise PackageShapeRecordError(f"{field} has an invalid enum value") from exc
    if type(value) is not annotation:
        raise PackageShapeRecordError(f"{field} has an invalid primitive type")
    if annotation is str and not value:
        raise PackageShapeRecordError(f"{field} is missing")
    return value


def _decode_record_dto(value: object, dto_type: type, *, field: str) -> object:
    schema = _RECORD_FIELD_SCHEMAS[dto_type]
    required = frozenset(name for name, _ in schema)
    if type(value) is not dict or frozenset(value) != required:
        raise PackageShapeRecordError(f"{field} has unknown or missing fields")
    try:
        decoded = dto_type(
            **{
                name: _decode_declared_record_value(
                    value[name],
                    annotation,
                    field=f"{field}.{name}",
                )
                for name, annotation in schema
            }
        )
    except PackageShapeRecordError:
        raise
    except PackageShapeError as exc:
        raise PackageShapeRecordError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise PackageShapeRecordError(f"{field} is invalid") from exc
    if _encode_record_dto(decoded) != value:
        raise PackageShapeRecordError(f"{field} is not canonical")
    return decoded


_RECORD_ENVELOPES = {
    "captured_package_shape": ("shape", CapturedPackageShapeRecord),
    "resolved_rollback_plan": ("plan", ResolvedRollbackPlanRecord),
}


def _encode_record(record_type: str, record: object) -> dict[str, object]:
    payload_name, dto_type = _RECORD_ENVELOPES[record_type]
    if type(record) is not dto_type:
        raise PackageShapeRecordError("record DTO type is unsupported")
    return {
        "schema_version": PACKAGE_SHAPE_RECORD_SCHEMA_VERSION,
        "record_type": record_type,
        payload_name: _encode_record_dto(record),
    }


def _decode_record(value: object, record_type: str) -> object:
    payload_name, dto_type = _RECORD_ENVELOPES[record_type]
    required = frozenset({"schema_version", "record_type", payload_name})
    if type(value) is not dict or frozenset(value) != required:
        raise PackageShapeRecordError("record has unknown or missing fields")
    version = value["schema_version"]
    if type(version) is not int or version != PACKAGE_SHAPE_RECORD_SCHEMA_VERSION:
        raise PackageShapeRecordError("package-shape record schema version is unsupported")
    if type(value["record_type"]) is not str or value["record_type"] != record_type:
        raise PackageShapeRecordError("package-shape record type is unsupported")
    return _decode_record_dto(value[payload_name], dto_type, field=payload_name)


def _provider_to_record(provider: DistributionProvider) -> DistributionProviderRecord:
    return DistributionProviderRecord(
        name=provider.name,
        version=provider.version,
        provider_id=provider.provider_id,
        requires_dist=provider.requires_dist,
    )


def _captured_to_record(
    captured: CapturedPackageShape | CapturedPackageShapeRecord,
) -> CapturedPackageShapeRecord:
    if isinstance(captured, CapturedPackageShapeRecord):
        return captured
    if not isinstance(captured, CapturedPackageShape):
        raise PackageShapeRecordError("captured package shape has an unsupported type")
    return CapturedPackageShapeRecord(
        core_provider=_provider_to_record(captured.core_provider),
        launcher=_ServiceLauncherRecord(
            python=captured.launcher.python,
            main=captured.launcher.main,
        ),
        release_family=captured.release_family,
        memory_providers=tuple(_provider_to_record(item) for item in captured.memory_providers),
        transition_memory_version=captured.transition_memory_version,
        residual_memory=captured.residual_memory,
    )


def encode_captured_package_shape_record(
    captured: CapturedPackageShape | CapturedPackageShapeRecord,
) -> dict[str, object]:
    """Encode captured evidence into a versioned JSON-safe object."""

    return _encode_record("captured_package_shape", _captured_to_record(captured))


def decode_captured_package_shape_record(value: object) -> CapturedPackageShapeRecord:
    """Decode captured evidence without consulting installed package state."""

    record = _decode_record(value, "captured_package_shape")
    assert isinstance(record, CapturedPackageShapeRecord)
    return record


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


def encode_resolved_rollback_plan_record(
    plan: ResolvedRollbackPlan | ResolvedRollbackPlanRecord,
) -> dict[str, object]:
    """Encode a plan as data that cannot itself authorize execution."""

    return _encode_record("resolved_rollback_plan", _plan_to_record(plan))


def decode_resolved_rollback_plan_record(value: object) -> ResolvedRollbackPlanRecord:
    """Decode plan data without constructing an executable rollback plan."""

    record = _decode_record(value, "resolved_rollback_plan")
    assert isinstance(record, ResolvedRollbackPlanRecord)
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
    try:
        _ProviderInventoryRecord(providers=tuple(_provider_to_record(provider) for provider in observed))
    except PackageShapeRecordError as exc:
        raise PackageShapeError(str(exc)) from exc
    relevant = tuple({provider.provider_id: provider for provider in observed}.values())

    by_name: dict[str, list[DistributionProvider]] = {}
    for provider in relevant:
        by_name.setdefault(provider.name, []).append(provider)
    duplicate = next((name for name, values in by_name.items() if len(values) > 1), None)
    if duplicate is not None:
        raise DuplicateDistributionProviderError(f"multiple canonical {duplicate} dist-info providers are installed")

    installed_core_providers = tuple(provider for provider in relevant if provider.name in CORE_DISTRIBUTION_NAMES)
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
    memory_providers = tuple(provider for provider in relevant if provider.name == MEMORY_PACKAGE_NAME)
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
            raise RollbackResolutionError("transition package shape is missing its exact Memory distribution")
        if captured.memory_version != captured.transition_memory_version:
            raise RollbackResolutionError("transition package shape does not match its exact Memory dependency")

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


def _rollback_uninstall_distributions() -> tuple[str, ...]:
    return tuple(sorted((*CORE_DISTRIBUTION_NAMES, MEMORY_PACKAGE_NAME)))


def _package_shape_verification(
    captured: CapturedPackageShape | CapturedPackageShapeRecord,
) -> PackageShapeVerification:
    return PackageShapeVerification(
        core_distribution=captured.core_distribution,
        core_version=captured.core_version,
        absent_core_distributions=tuple(sorted(CORE_DISTRIBUTION_NAMES - {captured.core_distribution})),
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
    try:
        _plan_to_record(plan)
    except PackageShapeRecordError as exc:
        raise RollbackResolutionError(str(exc)) from exc
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
            requirement for requirement in requirements if requirement.distribution != MEMORY_PACKAGE_NAME
        )
        memory_requirements = tuple(
            requirement for requirement in requirements if requirement.distribution == MEMORY_PACKAGE_NAME
        )
        resolver_requests = (
            (False, core_requirements),
            (True, memory_requirements),
        )
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("PIP_") and key != "PYTHONPATH"},
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
                raise RollbackResolutionError("exact rollback requirements did not resolve from the local wheelhouse")
        temporary_artifacts = _staged_artifacts(temporary)
        _construct_resolved_plan(
            captured=captured,
            requirements=requirements,
            artifacts=temporary_artifacts,
            staging_dir=temporary,
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
