"""Strict persisted DTO codec evidence for package-shape recovery records."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import vibe.package_shape as package_shape
from vibe.package_shape import (
    CapturedPackageShapeRecord,
    DistributionProvider,
    ExactRequirement,
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
            path=staging_dir / f"{requirement.distribution}-{requirement.version}.whl",
            sha256=("a" if index == 0 else "b") * 64,
            requires_dist=("dependency==1",) if index == 0 else (),
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


def test_captured_record_codec_is_json_safe_immutable_and_lossless() -> None:
    encoded = _json_round_trip(encode_captured_package_shape_record(_captured()))

    decoded = decode_captured_package_shape_record(encoded)

    assert isinstance(decoded, CapturedPackageShapeRecord)
    assert encode_captured_package_shape_record(decoded) == encoded
    assert decoded.core_provider.provider_id.endswith("avibe_os-3.0.14.dist-info")
    assert decoded.memory_providers[0].version == "3.0.15"
    with pytest.raises(FrozenInstanceError):
        decoded.residual_memory = True  # type: ignore[misc]


def test_resolved_record_codec_preserves_complete_non_executable_plan(
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
def test_record_codec_rejects_unknown_or_non_integer_schema_versions(
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
def test_captured_decoder_rejects_structural_drift(mutation: object) -> None:
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
def test_resolved_decoder_rejects_structural_drift(
    mutation: object,
    tmp_path: Path,
) -> None:
    payload = deepcopy(encode_resolved_rollback_plan_record(_resolved(tmp_path)))
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(PackageShapeRecordError):
        decode_resolved_rollback_plan_record(payload)


def test_resolved_decoder_rejects_unknown_schema_version(tmp_path: Path) -> None:
    payload = encode_resolved_rollback_plan_record(_resolved(tmp_path))
    payload["schema_version"] = 2

    with pytest.raises(PackageShapeRecordError, match="schema version"):
        decode_resolved_rollback_plan_record(payload)


def test_decoded_record_reencodes_without_live_files(tmp_path: Path) -> None:
    payload = encode_resolved_rollback_plan_record(_resolved(tmp_path))
    decoded = decode_resolved_rollback_plan_record(_json_round_trip(payload))

    assert not (tmp_path / "staging").exists()
    assert encode_resolved_rollback_plan_record(decoded) == payload
