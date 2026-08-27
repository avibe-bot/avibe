"""MEMORY-INDEP-019 exact capture and resolver-satisfiable rollback evidence."""

from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import subprocess
import zipfile
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any

import pytest

from vibe.package_shape import (
    CapturedPackageShape,
    DistributionProvider,
    DuplicateDistributionProviderError,
    PackageShapeError,
    ReleaseFamily,
    ResolvedRollbackPlan,
    RollbackResolutionError,
    capture_package_shape,
    inspect_installed_distribution_providers,
    resolve_rollback_plan,
)
from vibe.runtime import ServiceLauncher
from vibe.upgrade import build_upgrade_plan


ROOT = Path(__file__).resolve().parents[3]
SHIPPED_SOURCE_ROOTS = ("main.py", "config", "core", "modules", "storage", "vibe")
LAUNCHER = ServiceLauncher(
    python="/opt/avibe/bin/python",
    main="/opt/avibe/lib/python/site-packages/vibe/service_main.py",
)
OPTIONAL_MEMORY_REQUIREMENT = 'avibe-memory>=3.0.14.dev0,<3.1; extra == "memory"'


def _provider(
    name: str,
    version: str,
    *,
    requires_dist: tuple[str, ...] = (),
    suffix: str = "1",
) -> DistributionProvider:
    return DistributionProvider(
        name=name,
        version=version,
        provider_id=f"/{name}-{version}-{suffix}.dist-info",
        requires_dist=requires_dist,
    )


def _shape(
    core_version: str,
    *,
    memory_version: str | None,
    core_requires: tuple[str, ...] = (OPTIONAL_MEMORY_REQUIREMENT,),
) -> CapturedPackageShape:
    providers = [_provider("avibe-os", core_version, requires_dist=core_requires)]
    if memory_version is not None:
        providers.append(_provider("avibe-memory", memory_version))
    return capture_package_shape(
        core_version=core_version,
        launcher=LAUNCHER,
        providers=providers,
    )


def _wheel(
    wheelhouse: Path,
    name: str,
    version: str,
    *,
    requires_dist: tuple[str, ...] = (),
) -> Path:
    wheel_name = name.replace("-", "_")
    dist_info = f"{wheel_name}-{version}.dist-info"
    path = wheelhouse / f"{wheel_name}-{version}-py3-none-any.whl"
    metadata = "\n".join(
        [
            "Metadata-Version: 2.1",
            f"Name: {name}",
            f"Version: {version}",
            *(f"Requires-Dist: {requirement}" for requirement in requires_dist),
            "",
        ]
    ).encode()
    wheel = b"Wheel-Version: 1.0\nGenerator: memory-indep-019\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    entries = {
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": wheel,
    }
    rows: list[list[str]] = []
    for entry, payload in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        rows.append([entry, f"sha256={digest}", str(len(payload))])
    record_name = f"{dist_info}/RECORD"
    rows.append([record_name, "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[record_name] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry, payload in entries.items():
            archive.writestr(entry, payload)
    return path


@dataclass(frozen=True)
class FamilyCase:
    name: str
    core_version: str
    memory_version: str | None
    core_requires: tuple[str, ...]
    expected_family: ReleaseFamily
    expected_requirements: tuple[str, ...] | None
    residual_memory: bool = False


FAMILY_CASES = (
    FamilyCase(
        "core_only",
        "3.0.14",
        None,
        (OPTIONAL_MEMORY_REQUIREMENT,),
        ReleaseFamily.OPTIONAL_SPLIT,
        ("avibe-os==3.0.14",),
    ),
    FamilyCase(
        "matching_split",
        "3.0.14",
        "3.0.14",
        (OPTIONAL_MEMORY_REQUIREMENT,),
        ReleaseFamily.OPTIONAL_SPLIT,
        ("avibe-os==3.0.14", "avibe-memory==3.0.14"),
    ),
    FamilyCase(
        "optional_mismatch",
        "3.0.14",
        "3.0.15",
        (OPTIONAL_MEMORY_REQUIREMENT,),
        ReleaseFamily.OPTIONAL_SPLIT,
        ("avibe-os==3.0.14", "avibe-memory==3.0.15"),
    ),
    FamilyCase(
        "healthy_transition",
        "3.1.0",
        "3.1.0",
        ("avibe-memory==3.1.0",),
        ReleaseFamily.TRANSITION,
        ("avibe-os==3.1.0", "avibe-memory==3.1.0"),
    ),
    FamilyCase(
        "transition_missing",
        "3.1.0",
        None,
        ("avibe-memory==3.1.0",),
        ReleaseFamily.TRANSITION,
        None,
    ),
    FamilyCase(
        "transition_mismatch",
        "3.1.0",
        "3.1.1",
        ("avibe-memory==3.1.0",),
        ReleaseFamily.TRANSITION,
        None,
    ),
    FamilyCase(
        "pre_split",
        "3.0.13",
        None,
        (),
        ReleaseFamily.PRE_SPLIT,
        ("avibe-os==3.0.13",),
    ),
    FamilyCase(
        "bundled_residual",
        "3.0.13",
        "3.0.12",
        (),
        ReleaseFamily.PRE_SPLIT,
        ("avibe-os==3.0.13", "avibe-memory==3.0.12"),
        residual_memory=True,
    ),
)


@pytest.mark.parametrize("case", FAMILY_CASES, ids=lambda case: case.name)
def test_memory_indep_019_release_family_property_table(
    case: FamilyCase,
    tmp_path: Path,
) -> None:
    """Every specified family either resolves exactly or has no plan."""

    shape = _shape(
        case.core_version,
        memory_version=case.memory_version,
        core_requires=case.core_requires,
    )
    assert shape.release_family is case.expected_family
    assert shape.memory_present is (case.memory_version is not None)
    assert shape.memory_version == case.memory_version
    assert shape.memory_provider_cardinality == int(case.memory_version is not None)
    assert shape.residual_memory is case.residual_memory

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _wheel(
        wheelhouse,
        "avibe-os",
        case.core_version,
        requires_dist=case.core_requires,
    )
    if case.memory_version is not None:
        _wheel(wheelhouse, "avibe-memory", case.memory_version)
    staging = tmp_path / "staging"
    if case.expected_requirements is None:
        with pytest.raises(RollbackResolutionError):
            resolve_rollback_plan(shape, wheelhouse=wheelhouse, staging_dir=staging)
        assert not staging.exists()
        return

    plan = resolve_rollback_plan(shape, wheelhouse=wheelhouse, staging_dir=staging)
    assert tuple(requirement.specifier for requirement in plan.requirements) == (
        case.expected_requirements
    )
    assert plan.verification.memory_provider_cardinality == int(case.memory_version is not None)
    assert plan.verification.memory_version == case.memory_version
    assert plan.verification.residual_memory is case.residual_memory
    assert plan.verification.absent_core_distributions == ("vibe-remote",)
    assert plan.staging_dir == staging
    assert all(artifact.path.parent == staging for artifact in plan.artifacts)
    assert all(len(artifact.sha256) == 64 for artifact in plan.artifacts)


def test_memory_indep_019_capture_normalizes_versions_and_is_immutable() -> None:
    shape = _shape("3.0.14RC1", memory_version="3.0.14RC1")
    assert shape.core_version == "3.0.14rc1"
    assert shape.memory_version == "3.0.14rc1"
    with pytest.raises(FrozenInstanceError):
        shape.residual_memory = True  # type: ignore[misc]


def test_memory_indep_019_unknown_post_split_family_fails_closed() -> None:
    with pytest.raises(PackageShapeError, match="known Memory release family"):
        _shape("3.1.1", memory_version=None, core_requires=())


def test_memory_indep_019_duplicate_canonical_providers_fail_before_shape() -> None:
    providers = (
        _provider("avibe-os", "3.0.14"),
        _provider("avibe-memory", "3.0.14", suffix="one"),
        _provider("avibe_memory", "3.0.14", suffix="two"),
    )
    with pytest.raises(DuplicateDistributionProviderError):
        capture_package_shape(
            core_version="3.0.14",
            launcher=LAUNCHER,
            providers=providers,
        )


@pytest.mark.parametrize(
    ("core_version", "expected_provider", "expected_family"),
    [
        ("3.0.13", "vibe-remote", ReleaseFamily.PRE_SPLIT),
        ("3.0.14", "avibe-os", ReleaseFamily.OPTIONAL_SPLIT),
    ],
)
def test_memory_indep_019_alternate_core_providers_select_running_version(
    core_version: str,
    expected_provider: str,
    expected_family: ReleaseFamily,
) -> None:
    providers = (
        _provider("vibe-remote", "3.0.13"),
        _provider(
            "avibe-os",
            "3.0.14",
            requires_dist=(OPTIONAL_MEMORY_REQUIREMENT,),
        ),
    )
    shape = capture_package_shape(
        core_version=core_version,
        launcher=LAUNCHER,
        providers=providers,
    )
    assert shape.core_distribution == expected_provider
    assert shape.release_family is expected_family


def test_memory_indep_019_alternate_core_providers_reject_ambiguous_version() -> None:
    providers = (
        _provider("vibe-remote", "3.0.13"),
        _provider("avibe-os", "3.0.13"),
    )
    with pytest.raises(PackageShapeError, match="exactly one"):
        capture_package_shape(
            core_version="3.0.13",
            launcher=LAUNCHER,
            providers=providers,
        )


class _UnreadableDistribution:
    @property
    def metadata(self) -> Any:
        raise OSError("unreadable metadata")


def test_memory_indep_019_unreadable_metadata_fails_before_shape() -> None:
    with pytest.raises(PackageShapeError, match="unreadable"):
        inspect_installed_distribution_providers([_UnreadableDistribution()])


def test_memory_indep_019_resolver_failure_leaves_no_plan_or_staging(tmp_path: Path) -> None:
    shape = _shape("3.0.14", memory_version=None)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    staging = tmp_path / "staging"
    with pytest.raises(RollbackResolutionError, match="did not resolve"):
        resolve_rollback_plan(shape, wheelhouse=wheelhouse, staging_dir=staging)
    assert not staging.exists()


def test_memory_indep_019_core_only_rejects_transitive_memory_artifact(tmp_path: Path) -> None:
    core_requires = (
        "shape-helper==1.0",
        OPTIONAL_MEMORY_REQUIREMENT,
    )
    shape = _shape("3.0.14", memory_version=None, core_requires=core_requires)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _wheel(wheelhouse, "avibe-os", "3.0.14", requires_dist=core_requires)
    _wheel(
        wheelhouse,
        "shape-helper",
        "1.0",
        requires_dist=("avibe-memory==3.0.14",),
    )
    _wheel(wheelhouse, "avibe-memory", "3.0.14")
    staging = tmp_path / "staging"
    with pytest.raises(RollbackResolutionError, match="exact captured Memory shape"):
        resolve_rollback_plan(shape, wheelhouse=wheelhouse, staging_dir=staging)
    assert not staging.exists()


def test_memory_indep_019_local_resolution_only_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _shape("3.0.14", memory_version="3.0.15")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _wheel(
        wheelhouse,
        "avibe-os",
        "3.0.14",
        requires_dist=(OPTIONAL_MEMORY_REQUIREMENT,),
    )
    _wheel(wheelhouse, "avibe-memory", "3.0.15")
    commands: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def record_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        return real_run(command, **kwargs)

    monkeypatch.setattr("vibe.package_shape.subprocess.run", record_run)
    resolve_rollback_plan(
        shape,
        wheelhouse=wheelhouse,
        staging_dir=tmp_path / "staging",
    )
    assert len(commands) == 1
    assert commands[0][1:4] == ("-m", "pip", "download")
    assert "install" not in commands[0]


def test_memory_indep_019_resolved_plan_constructor_cannot_be_bypassed() -> None:
    with pytest.raises(TypeError, match="constructed only by rollback resolution"):
        ResolvedRollbackPlan(
            captured=_shape("3.0.14", memory_version=None),
            requirements=(),
            artifacts=(),
            staging_dir=Path("/not/staged"),
            uninstall_distributions=(),
            verification=None,  # type: ignore[arg-type]
        )


def test_memory_indep_019_unknown_hybrid_family_fails_closed() -> None:
    with pytest.raises(PackageShapeError, match="one exact Memory dependency"):
        _shape(
            "3.1.0",
            memory_version="3.1.0",
            core_requires=(
                "avibe-memory==3.1.0",
                'avibe-memory>=3.0.14.dev0,<3.1; extra == "memory"',
            ),
        )


def _call_inventory(function_name: str) -> dict[str, int]:
    callers: dict[str, int] = {}
    for root in SHIPPED_SOURCE_ROOTS:
        target = ROOT / root
        for source in [target] if target.is_file() else sorted(target.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            calls = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
                == function_name
            )
            if calls:
                callers[source.relative_to(ROOT).as_posix()] = calls
    return callers


def test_memory_indep_019_full_tree_caller_inventory_keeps_ownership_private() -> None:
    assert _call_inventory("capture_installed_package_shape") == {"vibe/upgrade.py": 1}
    assert _call_inventory("resolve_rollback_plan") == {}
    assert _call_inventory("ResolvedRollbackPlan") == {}


def test_memory_indep_019_pinned_core_and_memory_requirements_are_independent(monkeypatch) -> None:
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: True)
    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        version="3.0.14",
        package_name="avibe-os",
        memory_package=True,
        memory_version="3.0.15",
    )
    assert "avibe-os==3.0.14" in plan.command
    assert "avibe-os[memory]" not in " ".join(plan.command)
    assert "avibe-memory==3.0.15" in plan.command


def test_memory_indep_019_no_resolved_target_has_presence_without_version(tmp_path: Path) -> None:
    shape = _shape("3.0.14", memory_version=None)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _wheel(
        wheelhouse,
        "avibe-os",
        "3.0.14",
        requires_dist=(OPTIONAL_MEMORY_REQUIREMENT,),
    )
    plan = resolve_rollback_plan(
        shape,
        wheelhouse=wheelhouse,
        staging_dir=tmp_path / "staging",
    )
    assert plan.captured.memory_package is False
    assert plan.captured.memory_version is None


def test_memory_indep_019_legacy_target_marks_presence_without_version_unavailable() -> None:
    from vibe.upgrade import RollbackTarget

    target = RollbackTarget(
        version="3.0.14",
        package="avibe-os",
        launcher=LAUNCHER,
        memory_package=True,
        memory_version=None,
    )
    assert not target
    assert target.memory_package is False
    assert target.memory_version is None
