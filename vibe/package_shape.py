"""Exact pre-mutation package capture and staged rollback resolution."""

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
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from vibe.runtime import ServiceLauncher


CORE_PACKAGE_NAME = "avibe-os"
LEGACY_CORE_PACKAGE_NAME = "vibe-remote"
MEMORY_PACKAGE_NAME = "avibe-memory"
CORE_DISTRIBUTION_NAMES = frozenset({CORE_PACKAGE_NAME, LEGACY_CORE_PACKAGE_NAME})
MEMORY_SPLIT_MIN_VERSION = Version("3.0.14.dev0")


class PackageShapeError(ValueError):
    """The installed package shape cannot be represented safely."""


class DuplicateDistributionProviderError(PackageShapeError):
    """More than one dist-info provider claims one canonical distribution."""


class RollbackResolutionError(PackageShapeError):
    """An exact rollback target could not be resolved entirely into staging."""


class ReleaseFamily(str, Enum):
    PRE_SPLIT = "pre_split"
    OPTIONAL_SPLIT = "optional_split"
    TRANSITION = "transition"


def _normalized_version(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackageShapeError(f"{field} is missing")
    try:
        return str(Version(value.strip()))
    except InvalidVersion as exc:
        raise PackageShapeError(f"{field} is not a valid package version") from exc


@dataclass(frozen=True, order=True)
class DistributionProvider:
    """One exact installed dist-info provider."""

    name: str
    version: str
    provider_id: str
    requires_dist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        canonical_name = canonicalize_name(self.name)
        if not canonical_name:
            raise PackageShapeError("distribution provider name is missing")
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise PackageShapeError("distribution provider identity is missing")
        object.__setattr__(self, "name", canonical_name)
        object.__setattr__(
            self,
            "version",
            _normalized_version(self.version, field=f"{canonical_name} version"),
        )
        object.__setattr__(self, "provider_id", self.provider_id.strip())
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
        if not isinstance(self.release_family, ReleaseFamily):
            raise PackageShapeError("captured release family is unknown")
        if self.core_provider.name not in CORE_DISTRIBUTION_NAMES:
            raise PackageShapeError("captured core provider is not an Avibe distribution")
        if not isinstance(self.launcher, ServiceLauncher):
            raise PackageShapeError("captured service launcher is invalid")
        if len(self.memory_providers) > 1:
            raise DuplicateDistributionProviderError(
                "multiple canonical avibe-memory providers are not representable"
            )
        if any(provider.name != MEMORY_PACKAGE_NAME for provider in self.memory_providers):
            raise PackageShapeError("captured Memory provider has the wrong canonical name")

        transition_memory_version = self.transition_memory_version
        if transition_memory_version is not None:
            transition_memory_version = _normalized_version(
                transition_memory_version,
                field="transition Memory requirement",
            )
        object.__setattr__(self, "memory_providers", tuple(self.memory_providers))
        object.__setattr__(self, "transition_memory_version", transition_memory_version)

        if self.release_family is ReleaseFamily.TRANSITION:
            if transition_memory_version is None:
                raise PackageShapeError("transition family is missing its exact Memory requirement")
        elif transition_memory_version is not None:
            raise PackageShapeError("only the transition family may carry a hard Memory requirement")

        expected_residual = self.release_family is ReleaseFamily.PRE_SPLIT and self.memory_present
        if self.residual_memory != expected_residual:
            raise PackageShapeError("residual Memory marker does not match the captured family")

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
        canonical_name = canonicalize_name(self.distribution)
        if not canonical_name:
            raise PackageShapeError("rollback requirement distribution is missing")
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


def _parsed_memory_requirements(provider: DistributionProvider) -> tuple[Requirement, ...]:
    parsed: list[Requirement] = []
    for value in provider.requires_dist:
        try:
            requirement = Requirement(value)
        except (InvalidRequirement, TypeError) as exc:
            raise PackageShapeError("core distribution contains unreadable dependency metadata") from exc
        if canonicalize_name(requirement.name) == MEMORY_PACKAGE_NAME:
            parsed.append(requirement)
    return tuple(parsed)


def _release_family(provider: DistributionProvider) -> tuple[ReleaseFamily, str | None]:
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
    relevant = tuple(
        provider
        for provider in providers
        if provider.name in CORE_DISTRIBUTION_NAMES or provider.name == MEMORY_PACKAGE_NAME
    )
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
    if len(installed_core_providers) > 1:
        raise DuplicateDistributionProviderError(
            "multiple canonical core distribution providers are installed"
        )
    if not installed_core_providers:
        raise PackageShapeError("running core has no canonical distribution provider")

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
            canonical_name = canonicalize_name(name)
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


def _rollback_requirements(captured: CapturedPackageShape) -> tuple[ExactRequirement, ...]:
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
    if provider.name != canonicalize_name(str(filename_name)) or provider.version != str(filename_version):
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
    captured: CapturedPackageShape,
    artifacts: tuple[StagedArtifact, ...],
) -> None:
    by_distribution: dict[str, list[StagedArtifact]] = {}
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
        tuple(sorted((*CORE_DISTRIBUTION_NAMES, MEMORY_PACKAGE_NAME))),
    )
    object.__setattr__(
        plan,
        "verification",
        PackageShapeVerification(
            core_distribution=captured.core_distribution,
            core_version=captured.core_version,
            absent_core_distributions=tuple(
                sorted(CORE_DISTRIBUTION_NAMES - {captured.core_distribution})
            ),
            memory_provider_cardinality=captured.memory_provider_cardinality,
            memory_version=captured.memory_version,
            residual_memory=captured.residual_memory,
        ),
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
    command = [
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
        *(requirement.specifier for requirement in requirements),
    ]
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
    "DistributionProvider",
    "DuplicateDistributionProviderError",
    "ExactRequirement",
    "PackageShapeError",
    "PackageShapeVerification",
    "ReleaseFamily",
    "ResolvedRollbackPlan",
    "RollbackResolutionError",
    "StagedArtifact",
    "capture_installed_package_shape",
    "capture_package_shape",
    "inspect_installed_distribution_providers",
    "resolve_rollback_plan",
]
