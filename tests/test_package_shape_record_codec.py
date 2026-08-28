"""Strict persisted DTO codec evidence for package-shape recovery records."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibe.package_shape as package_shape
from vibe.package_shape import (
    CapturedPackageShapeRecord,
    DistributionProvider,
    ExactRequirement,
    PackageShapeError,
    PackageShapeRecordError,
    ReleaseFamily,
    ResolvedRollbackPlan,
    ResolvedRollbackPlanRecord,
    StagedArtifact,
    decode_captured_package_shape_record,
    decode_resolved_rollback_plan_record,
    encode_captured_package_shape_record,
    encode_resolved_rollback_plan_record,
)
from vibe.runtime import ServiceLauncher
from vibe.upgrade import rollback_target


def _captured() -> package_shape.CapturedPackageShape:
    return package_shape.CapturedPackageShape(
        core_provider=DistributionProvider(
            name="avibe-os",
            version="3.0.14",
            provider_id="/opt/avibe/avibe_os-3.0.14.dist-info",
            requires_dist=('avibe-memory>=3.0.14.dev0,<3.1; extra == "memory"',),
        ),
        launcher=ServiceLauncher(
            python="/opt/avibe/bin/python",
            main="/opt/avibe/site-packages/vibe/service_main.py",
        ),
        release_family=ReleaseFamily.OPTIONAL_SPLIT,
        memory_providers=(
            DistributionProvider(
                name="avibe-memory",
                version="3.0.15",
                provider_id="/opt/avibe/avibe_memory-3.0.15.dist-info",
            ),
        ),
        transition_memory_version=None,
        residual_memory=False,
    )


def _resolved(tmp_path: Path) -> ResolvedRollbackPlan:
    captured = _captured()
    staging_dir = tmp_path / "staging"
    requirements = (
        ExactRequirement("avibe-os", "3.0.14"),
        ExactRequirement("avibe-memory", "3.0.15"),
    )
    artifacts = tuple(
        StagedArtifact(
            distribution=requirement.distribution,
            version=requirement.version,
            path=(staging_dir / f"{requirement.distribution.replace('-', '_')}-{requirement.version}-py3-none-any.whl"),
            sha256=("a" if index == 0 else "b") * 64,
            requires_dist=(captured.core_provider.requires_dist if index == 0 else ()),
        )
        for index, requirement in enumerate(requirements)
    )
    return package_shape._construct_resolved_plan(
        captured=captured,
        requirements=requirements,
        artifacts=artifacts,
        staging_dir=staging_dir,
    )


def _json_round_trip(value: dict[str, object]) -> dict[str, object]:
    loaded = json.loads(json.dumps(value))
    assert isinstance(loaded, dict)
    return loaded


def test_memory_indep_019_captured_codec_is_json_safe_immutable_and_lossless() -> None:
    encoded = _json_round_trip(encode_captured_package_shape_record(_captured()))

    decoded = decode_captured_package_shape_record(encoded)

    assert isinstance(decoded, CapturedPackageShapeRecord)
    assert encode_captured_package_shape_record(decoded) == encoded
    assert decoded.core_provider.provider_id.endswith("avibe_os-3.0.14.dist-info")
    assert decoded.memory_providers[0].version == "3.0.15"
    with pytest.raises(FrozenInstanceError):
        decoded.residual_memory = True  # type: ignore[misc]


def test_memory_indep_019_resolved_codec_preserves_complete_non_executable_plan(
    tmp_path: Path,
) -> None:
    encoded = _json_round_trip(encode_resolved_rollback_plan_record(_resolved(tmp_path)))

    decoded = decode_resolved_rollback_plan_record(encoded)

    assert isinstance(decoded, ResolvedRollbackPlanRecord)
    assert not isinstance(decoded, ResolvedRollbackPlan)
    assert encode_resolved_rollback_plan_record(decoded) == encoded
    assert decoded.staging_dir == str(tmp_path / "staging")
    assert [artifact.sha256 for artifact in decoded.artifacts] == ["a" * 64, "b" * 64]
    assert decoded.verification.memory_version == "3.0.15"
    with pytest.raises(TypeError, match="constructed only by rollback resolution"):
        ResolvedRollbackPlan(**decoded.__dict__)


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_memory_indep_019_codec_rejects_unknown_or_non_integer_schema_versions(
    version: object,
) -> None:
    payload = encode_captured_package_shape_record(_captured())
    payload["schema_version"] = version

    with pytest.raises(PackageShapeRecordError, match="schema version"):
        decode_captured_package_shape_record(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra="unknown"),
        lambda value: value.pop("record_type"),
        lambda value: value.update(record_type="resolved_rollback_plan"),
        lambda value: value["shape"].update(extra="unknown"),
        lambda value: value["shape"].pop("launcher"),
        lambda value: value["shape"]["core_provider"].update(version=3),
        lambda value: value["shape"].update(memory_providers="not-a-list"),
        lambda value: value["shape"].update(residual_memory=1),
        lambda value: value["shape"].update(release_family="transition"),
        lambda value: value["shape"].update(residual_memory=True),
    ],
)
def test_memory_indep_019_captured_decoder_rejects_structural_drift(
    mutation: object,
) -> None:
    payload = deepcopy(encode_captured_package_shape_record(_captured()))
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(PackageShapeRecordError):
        decode_captured_package_shape_record(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra="unknown"),
        lambda value: value["plan"].pop("verification"),
        lambda value: value["plan"]["requirements"][0].update(extra="unknown"),
        lambda value: value["plan"]["artifacts"][0].update(sha256="not-a-digest"),
        lambda value: value["plan"]["artifacts"][0].update(path=4),
        lambda value: value["plan"].update(uninstall_distributions=["Avibe_OS"]),
        lambda value: value["plan"]["verification"].update(memory_provider_cardinality=True),
        lambda value: value["plan"]["verification"].update(core_version="3.0.13"),
        lambda value: value["plan"]["requirements"][0].update(version="3.0.13"),
        lambda value: value["plan"].update(requirements="not-a-list"),
    ],
)
def test_memory_indep_019_resolved_decoder_rejects_structural_drift(
    mutation: object,
    tmp_path: Path,
) -> None:
    payload = deepcopy(encode_resolved_rollback_plan_record(_resolved(tmp_path)))
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(PackageShapeRecordError):
        decode_resolved_rollback_plan_record(payload)


def test_memory_indep_019_resolved_decoder_rejects_unknown_schema(
    tmp_path: Path,
) -> None:
    payload = encode_resolved_rollback_plan_record(_resolved(tmp_path))
    payload["schema_version"] = 2

    with pytest.raises(PackageShapeRecordError, match="schema version"):
        decode_resolved_rollback_plan_record(payload)


def test_memory_indep_019_decoder_binds_closure_to_captured_target(
    tmp_path: Path,
) -> None:
    payload = encode_resolved_rollback_plan_record(_resolved(tmp_path))
    payload["plan"]["requirements"][0].update(  # type: ignore[index]
        distribution="unrelated",
        version="1",
    )
    payload["plan"]["artifacts"][0].update(  # type: ignore[index]
        distribution="unrelated",
        version="1",
        path=str(tmp_path / "staging" / "unrelated-1-py3-none-any.whl"),
    )

    with pytest.raises(PackageShapeRecordError, match="captured rollback target"):
        decode_resolved_rollback_plan_record(payload)


def test_memory_indep_019_decoder_rejects_duplicate_staged_distribution(
    tmp_path: Path,
) -> None:
    payload = encode_resolved_rollback_plan_record(_resolved(tmp_path))
    duplicate = deepcopy(payload["plan"]["artifacts"][0])  # type: ignore[index]
    duplicate["version"] = "3.0.13"
    duplicate["path"] = str(tmp_path / "staging" / "avibe_os-3.0.13-py3-none-any.whl")
    payload["plan"]["artifacts"].append(duplicate)  # type: ignore[index]

    with pytest.raises(PackageShapeRecordError, match="duplicate distribution"):
        decode_resolved_rollback_plan_record(payload)


def test_memory_indep_019_provider_fact_construction_rejects_invalid_state() -> None:
    record = decode_captured_package_shape_record(encode_captured_package_shape_record(_captured()))

    with pytest.raises(PackageShapeError, match="identity is missing"):
        replace(record.core_provider, provider_id="")


def test_memory_indep_019_decoder_derives_family_from_core_metadata() -> None:
    payload = encode_captured_package_shape_record(_captured())
    payload["shape"].update(  # type: ignore[union-attr]
        release_family="pre_split",
        residual_memory=True,
    )

    with pytest.raises(PackageShapeRecordError, match="release family"):
        decode_captured_package_shape_record(payload)


@pytest.mark.parametrize("invalid_name", ["/", "@"])
def test_memory_indep_019_rejects_invalid_distribution_names(
    invalid_name: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(PackageShapeError, match="name is invalid"):
        DistributionProvider(invalid_name, "1", f"/{invalid_name}.dist-info")
    with pytest.raises(PackageShapeError, match="name is invalid"):
        ExactRequirement(invalid_name, "1")

    payload = encode_resolved_rollback_plan_record(_resolved(tmp_path))
    payload["plan"]["artifacts"].append(  # type: ignore[index]
        {
            "distribution": invalid_name,
            "version": "1",
            "path": str(tmp_path / "staging" / "invalid.whl"),
            "sha256": "c" * 64,
            "requires_dist": [],
        }
    )
    with pytest.raises(PackageShapeRecordError, match="name is invalid"):
        decode_resolved_rollback_plan_record(payload)


def test_memory_indep_019_rollback_target_skips_unrelated_invalid_distribution_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launcher = ServiceLauncher(
        python="/opt/avibe/bin/python",
        main="/opt/avibe/site-packages/vibe/service_main.py",
    )
    unrelated = SimpleNamespace(metadata={"Name": "broken/name"})
    core = SimpleNamespace(
        metadata={"Name": "avibe-os"},
        version="3.0.14",
        _path=tmp_path / "avibe_os-3.0.14.dist-info",
        requires=('avibe-memory>=3.0.14.dev0,<3.1; extra == "memory"',),
    )
    monkeypatch.setattr("vibe.__version__", "3.0.14")
    monkeypatch.setattr("vibe.runtime.current_service_launcher", lambda: launcher)
    monkeypatch.setattr("importlib.metadata.distributions", lambda: (unrelated, core))

    target = rollback_target()

    assert target is not None
    assert target.core_provider.name == "avibe-os"
    assert target.core_provider.version == "3.0.14"


def test_memory_indep_019_rollback_target_rejects_relevant_invalid_distribution_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    relevant_but_invalid = SimpleNamespace(
        metadata={"Name": "avibe-os "},
        version="3.0.14",
        _path=tmp_path / "avibe_os-3.0.14.dist-info",
        requires=(),
    )
    monkeypatch.setattr("vibe.__version__", "3.0.14")
    monkeypatch.setattr(
        "vibe.runtime.current_service_launcher",
        lambda: ServiceLauncher(python="/opt/avibe/bin/python", main="/opt/avibe/vibe/service_main.py"),
    )
    monkeypatch.setattr("importlib.metadata.distributions", lambda: (relevant_but_invalid,))

    with pytest.raises(PackageShapeError, match="installed distribution name is invalid"):
        rollback_target()


def test_memory_indep_019_artifact_fact_construction_rejects_invalid_state(
    tmp_path: Path,
) -> None:
    record = decode_resolved_rollback_plan_record(encode_resolved_rollback_plan_record(_resolved(tmp_path)))

    with pytest.raises(PackageShapeError, match="SHA-256"):
        replace(record.artifacts[0], sha256="not-a-digest")


def test_memory_indep_019_dto_rejects_staged_core_family_disagreement(
    tmp_path: Path,
) -> None:
    record = decode_resolved_rollback_plan_record(encode_resolved_rollback_plan_record(_resolved(tmp_path)))
    staged_core = replace(
        record.artifacts[0],
        requires_dist=("avibe-memory==3.0.14",),
    )

    with pytest.raises(PackageShapeRecordError, match="captured release family"):
        replace(record, artifacts=(staged_core, *record.artifacts[1:]))


@pytest.mark.parametrize("target", ["staging_dir", "artifact_path"])
def test_memory_indep_019_dto_binds_artifacts_to_staging_directory(
    target: str,
    tmp_path: Path,
) -> None:
    record = decode_resolved_rollback_plan_record(encode_resolved_rollback_plan_record(_resolved(tmp_path)))

    with pytest.raises(PackageShapeRecordError, match="staging directory"):
        if target == "staging_dir":
            replace(record, staging_dir=str(tmp_path / "elsewhere"))
        else:
            artifact = replace(
                record.artifacts[0],
                path=tmp_path / record.artifacts[0].path.name,
            )
            replace(record, artifacts=(artifact, *record.artifacts[1:]))


def test_memory_indep_019_dto_requires_canonical_unique_artifact_paths(
    tmp_path: Path,
) -> None:
    record = decode_resolved_rollback_plan_record(encode_resolved_rollback_plan_record(_resolved(tmp_path)))

    with pytest.raises(PackageShapeRecordError, match="canonical absolute path"):
        replace(record, staging_dir=f"{record.staging_dir}{os.sep}.")
    with pytest.raises(PackageShapeRecordError, match="duplicate artifact paths"):
        replace(record, artifacts=(record.artifacts[0], record.artifacts[0]))


def test_memory_indep_019_dto_rejects_conflicting_provider_identity() -> None:
    record = decode_captured_package_shape_record(encode_captured_package_shape_record(_captured()))
    memory_provider = replace(
        record.memory_providers[0],
        provider_id=record.core_provider.provider_id,
    )

    with pytest.raises(PackageShapeRecordError, match="conflicting metadata"):
        replace(record, memory_providers=(memory_provider,))


def test_memory_indep_019_decoded_record_reencodes_without_live_files(
    tmp_path: Path,
) -> None:
    payload = encode_resolved_rollback_plan_record(_resolved(tmp_path))
    decoded = decode_resolved_rollback_plan_record(_json_round_trip(payload))

    assert not (tmp_path / "staging").exists()
    assert encode_resolved_rollback_plan_record(decoded) == payload


@pytest.mark.skipif(os.name == "nt", reason="symlink drift fixture requires POSIX symlinks")
def test_memory_indep_019_provider_decode_is_stable_across_symlink_drift(
    tmp_path: Path,
) -> None:
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    first_target.mkdir()
    second_target.mkdir()
    provider_link = tmp_path / "provider"
    provider_link.symlink_to(first_target, target_is_directory=True)
    captured = _captured()
    provider_id = str(provider_link / "avibe_os-3.0.14.dist-info")
    captured = replace(
        captured,
        core_provider=replace(captured.core_provider, provider_id=provider_id),
    )
    payload = _json_round_trip(encode_captured_package_shape_record(captured))

    decoded_before = decode_captured_package_shape_record(payload)
    provider_link.unlink()
    provider_link.symlink_to(second_target, target_is_directory=True)
    decoded_after = decode_captured_package_shape_record(payload)

    assert decoded_before == decoded_after
    assert decoded_after.core_provider.provider_id == provider_id
    assert encode_captured_package_shape_record(decoded_after) == payload


@pytest.mark.parametrize(
    "wheel_name",
    [
        "unrelated-3.0.14-py3-none-any.whl",
        "avibe_os-9-py3-none-any.whl",
    ],
)
def test_memory_indep_019_decoder_binds_artifact_identity_to_wheel_filename(
    wheel_name: str,
    tmp_path: Path,
) -> None:
    payload = encode_resolved_rollback_plan_record(_resolved(tmp_path))
    payload["plan"]["artifacts"][0]["path"] = str(  # type: ignore[index]
        tmp_path / "staging" / wheel_name
    )

    with pytest.raises(PackageShapeRecordError, match="filename and recorded identity"):
        decode_resolved_rollback_plan_record(payload)
