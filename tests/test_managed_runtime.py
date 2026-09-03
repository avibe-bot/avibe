from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core import managed_runtime
from core.git_runtime import GitRuntimeManager
from core.managed_runtime import (
    ManagedRuntimeArchive,
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
)
from avibe_memory import artifact as memory_artifact
from avibe_memory.artifact import MemoryArtifactManager, MemoryRuntimeActivationError
from avibe_memory.artifact_contract import COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON
from avibe_memory.provider_root import ProviderRootError
from vibe.model_hub_runtime.installer import EngineRuntimeManager


class FixtureRuntimeManager(ManagedRuntimeManager):
    def _binary_version(self, binary: Path | None) -> str | None:
        if binary is None:
            return None
        return binary.read_text(encoding="utf-8").strip()


def _write_subclass_runtime_fixture(
    tmp_path: Path,
    runtime_kind: str,
    *,
    bin_path: str | None = None,
    runtime_version: str | None = None,
) -> tuple[Path, Path]:
    binary_paths = {
        "git": "bin/git",
        "memory": "bin/python",
        "model-hub": "cli-proxy-api",
    }
    binary_path = bin_path or binary_paths[runtime_kind]
    runtime_version = runtime_version or {
        "git": "2.55.0",
        "memory": "1.2.3",
        "model-hub": "v7.2.95",
    }[runtime_kind]
    binary_payloads = {
        "git": f'#!/bin/sh\n[ "$1" = "--version" ] || exit 2\necho git version {runtime_version}\n'.encode(),
        "memory": b"#!/bin/sh\nexit 0\n",
        "model-hub": (
            f'#!/bin/sh\n[ "$1" = "--help" ] || exit 2\n'
            f'echo "CLIProxyAPI Version: {runtime_version.removeprefix("v")}" >&2\n'
        ).encode(),
    }
    binary_payload = binary_payloads[runtime_kind]
    archive = tmp_path / f"{runtime_kind}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo(binary_path)
        member.mode = 0o755
        member.size = len(binary_payload)
        tar.addfile(member, io.BytesIO(binary_payload))

    archive_payload = {
        "name": archive.name,
        "url": archive.as_uri(),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(binary_payload).hexdigest(),
        "size": archive.stat().st_size,
        "bin_path": binary_path,
    }
    platform_tag = managed_runtime.runtime_platform_tag()
    if runtime_kind == "git":
        payload = {
            "schema_version": 1,
            "git_version": runtime_version,
            "source": "fixture",
            "release_state": "published",
            "archives": {platform_tag: archive_payload},
        }
    elif runtime_kind == "memory":
        payload = {
            "schema_version": 1,
            "everos_version": runtime_version,
            "python_version": "3.12.12",
            "lock_sha256": "e6acc17e4c0969563d380326e90134965af0822259bb4a9adb4d54433e9737fe",
            "lock_id": "uv-lock-sha256:e6acc17e4c0969563d380326e90134965af0822259bb4a9adb4d54433e9737fe",
            "uv_version": "0.9.18",
            "source": "fixture",
            "release_state": "published",
            "provider_root_format": "everos-1.2.3",
            "compatible_provider_root_formats": [],
            "archives": {platform_tag: archive_payload},
        }
    else:
        asset_platform = "linux-amd64" if platform_tag == "linux-x64" else platform_tag
        payload = {
            "schema_version": 1,
            "name": "cliproxyapi",
            "version": runtime_version,
            "source": "router-for-me/CLIProxyAPI",
            "source_sha": "f71ec0eb6776854457892452cf28c47f0d658251",
            "release_tag": runtime_version,
            "license": "MIT",
            "assets": [
                {
                    **archive_payload,
                    "platform": asset_platform,
                    "size_bytes": archive_payload["size"],
                }
            ],
        }
        del payload["assets"][0]["size"]
    manifest = tmp_path / f"{runtime_kind}-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return archive, manifest


def _subclass_runtime_manager(
    tmp_path: Path,
    runtime_kind: str,
    manifest: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_dir: Path | None = None,
) -> ManagedRuntimeManager:
    runtime_dir = runtime_dir or tmp_path / f"{runtime_kind}-runtime"
    if runtime_kind == "git":
        return GitRuntimeManager(runtime_dir=runtime_dir, manifest_path=manifest)
    if runtime_kind == "memory":
        manager = MemoryArtifactManager(
            runtime_dir=runtime_dir,
            manifest_path=manifest,
            provider_root=tmp_path / "memory-provider-root",
        )
        monkeypatch.setattr(manager, "_prepare_binary", lambda _binary, **_kwargs: {"ok": True})
        monkeypatch.setattr(manager, "_binary_matches_manifest", lambda _binary, _manifest: True)
        return manager
    return EngineRuntimeManager(runtime_dir=runtime_dir, manifest_path=manifest)


def _change_unrelated_platform(manifest: Path, runtime_kind: str) -> None:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    unrelated = {
        "name": "unrelated.tar.gz",
        "url": "https://example.test/unrelated.tar.gz",
        "sha256": "d" * 64,
        "binary_sha256": "e" * 64,
        "size": 1,
        "bin_path": "unused",
    }
    if runtime_kind == "model-hub":
        payload["assets"].append(
            {
                **unrelated,
                "platform": "fixture-unrelated",
                "size_bytes": unrelated["size"],
            }
        )
        del payload["assets"][-1]["size"]
    else:
        payload["archives"]["fixture-unrelated"] = unrelated
    manifest.write_text(json.dumps(payload), encoding="utf-8")


def _resolve_subclass_runtime(manager: ManagedRuntimeManager, runtime_kind: str) -> Path | None:
    if runtime_kind == "git":
        return manager.resolve_git_path()  # type: ignore[attr-defined]
    if runtime_kind == "memory":
        return manager.resolve_python()  # type: ignore[attr-defined]
    return manager.resolve_engine_path()  # type: ignore[attr-defined]


def _fixture_runtime_manager(
    runtime_dir: Path,
    *,
    manifest_path: Path | None = None,
    manifest_url: str | None = None,
    offline: bool = False,
) -> FixtureRuntimeManager:
    return FixtureRuntimeManager(
        spec=ManagedRuntimeSpec(
            runtime_id="fixture",
            manifest_resource="unused.json",
            version_field="runtime_version",
            default_bin_path="bin/runtime",
        ),
        runtime_dir=runtime_dir,
        manifest_path=manifest_path,
        manifest_url=manifest_url,
        offline=offline,
    )


def test_force_install_uses_sibling_target_by_default(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="default-force",
        version="1.0.0",
    )
    manager = _fixture_runtime_manager(tmp_path / "runtime", manifest_path=manifest_path)
    installed = manager.ensure()

    refreshed = manager.ensure(force=True)

    assert refreshed["ok"] is True
    assert refreshed["install_dir"] != installed["install_dir"]
    assert Path(installed["install_dir"]).is_dir()


def test_force_candidate_is_validated_before_pointer_publication(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="validated-force",
        version="1.0.0",
    )
    manager = _fixture_runtime_manager(tmp_path / "runtime", manifest_path=manifest_path)
    installed = manager.ensure()
    pointer_path = manager.runtime_dir / "current.json"
    pointer_before = pointer_path.read_bytes()
    observed_candidate: Path | None = None

    def validate_candidate(candidate: Path) -> str | None:
        nonlocal observed_candidate
        observed_candidate = candidate
        assert candidate.is_file()
        assert candidate != Path(installed["path"])
        assert pointer_path.read_bytes() == pointer_before
        return None

    refreshed = manager.ensure(force=True, validate_candidate=validate_candidate)

    assert refreshed["ok"] is True
    assert observed_candidate == Path(refreshed["path"])
    assert pointer_path.read_bytes() != pointer_before
    assert Path(installed["path"]).is_file()


def test_force_candidate_rejection_preserves_published_install(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="rejected-force",
        version="1.0.0",
    )
    manager = _fixture_runtime_manager(tmp_path / "runtime", manifest_path=manifest_path)
    installed = manager.ensure()
    pointer_path = manager.runtime_dir / "current.json"
    pointer_before = pointer_path.read_bytes()
    versions_before = set((manager.runtime_dir / "versions").glob("**/install.json"))

    rejected = manager.ensure(
        force=True,
        validate_candidate=lambda _candidate: "fixture_candidate_validation_failed",
    )

    assert rejected["ok"] is False
    assert rejected["reason"] == "fixture_candidate_validation_failed"
    assert pointer_path.read_bytes() == pointer_before
    assert Path(installed["path"]).read_text(encoding="utf-8") == "1.0.0"
    assert set((manager.runtime_dir / "versions").glob("**/install.json")) == versions_before


def test_force_target_replacement_failure_does_not_publish_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="exact-force",
        version="1.0.0",
    )
    manager = FixtureRuntimeManager(
        spec=ManagedRuntimeSpec(
            runtime_id="fixture-exact-force",
            manifest_resource="unused.json",
            version_field="runtime_version",
            default_bin_path="bin/runtime",
            replace_target_on_force=True,
        ),
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    installed = manager.ensure()
    pointer_path = manager.runtime_dir / "current.json"
    pointer_before = pointer_path.read_bytes()
    binary_before = Path(installed["path"]).read_bytes()
    monkeypatch.setattr(manager, "_remove_install_target_for_replacement", lambda _path: False)

    failed = manager.ensure(force=True)

    assert failed["ok"] is False
    assert failed["reason"] == "fixture-exact-force_install_failed"
    assert pointer_path.read_bytes() == pointer_before
    assert Path(installed["path"]).read_bytes() == binary_before


def test_force_target_replacement_rejects_symlinked_canonical_leaf(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="symlink-force",
        version="1.0.0",
    )
    manager = FixtureRuntimeManager(
        spec=ManagedRuntimeSpec(
            runtime_id="fixture-symlink-force",
            manifest_resource="unused.json",
            version_field="runtime_version",
            default_bin_path="bin/runtime",
            replace_target_on_force=True,
        ),
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )
    installed = manager.ensure()
    canonical = Path(installed["install_dir"])
    pointer_path = manager.runtime_dir / "current.json"
    pointer_before = pointer_path.read_bytes()
    redirected = canonical.parent / "redirected"
    redirected.mkdir()
    sentinel = redirected / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    shutil.rmtree(canonical)
    canonical.symlink_to(redirected, target_is_directory=True)

    failed = manager.ensure(force=True)

    assert failed["ok"] is False
    assert failed["reason"] == "fixture-symlink-force_install_failed"
    assert canonical.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert pointer_path.read_bytes() == pointer_before


def _write_fixture_runtime_release(
    root: Path,
    manifest_path: Path,
    *,
    label: str,
    version: str,
    archive_name: str | None = None,
) -> Path:
    source_dir = root / "release-sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    archive_path = source_dir / (archive_name or f"fixture-{label}.tar.gz")
    binary_payload = version.encode("utf-8")
    with tarfile.open(archive_path, "w:gz") as archive_file:
        member = tarfile.TarInfo("bin/runtime")
        member.mode = 0o755
        member.size = len(binary_payload)
        archive_file.addfile(member, io.BytesIO(binary_payload))
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_version": version,
                "source": "fixture",
                "archives": {
                    managed_runtime.runtime_platform_tag(): {
                        "name": archive_path.name,
                        "url": archive_path.as_uri(),
                        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                        "binary_sha256": hashlib.sha256(binary_payload).hexdigest(),
                        "size": archive_path.stat().st_size,
                        "bin_path": "bin/runtime",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return archive_path


def test_binary_artifact_manifest_still_requires_binary_sha256(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="missing-binary-digest",
        version="1.0.0",
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = payload["archives"][managed_runtime.runtime_platform_tag()]
    archive.pop("binary_sha256")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manager = FixtureRuntimeManager(
        spec=ManagedRuntimeSpec(
            runtime_id="fixture-binary-digest",
            manifest_resource="unused.json",
            version_field="runtime_version",
            default_bin_path="bin/runtime",
        ),
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
    )

    result = manager.ensure()

    assert result["ok"] is False
    assert result["reason"] == "fixture-binary-digest_manifest_invalid"


def _install_fixture_runtime_release(
    manager: FixtureRuntimeManager,
    root: Path,
    manifest_path: Path,
    *,
    label: str,
    version: str,
    archive_name: str | None = None,
) -> tuple[Path, Path]:
    source_archive = _write_fixture_runtime_release(
        root,
        manifest_path,
        label=label,
        version=version,
        archive_name=archive_name,
    )
    with patch.object(manager, "_clean_after_successful_install"):
        installed = manager.ensure()
    assert installed["ok"] is True
    return Path(installed["install_dir"]), manager.runtime_dir / "downloads" / source_archive.name


def test_ensure_invokes_cleanup_only_after_publishing_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="published",
        version="1.0.0",
    )
    manager = _fixture_runtime_manager(tmp_path / "runtime", manifest_path=manifest_path)
    observed_install_dirs: list[str] = []

    def observe_cleanup() -> None:
        pointer = json.loads((manager.runtime_dir / "current.json").read_text(encoding="utf-8"))
        observed_install_dirs.append(pointer["install_dir"])

    monkeypatch.setattr(manager, "_clean_after_successful_install", observe_cleanup)

    result = manager.ensure()

    assert result["ok"] is True
    assert result["changed"] is True
    assert observed_install_dirs == [result["install_dir"]]

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert observed_install_dirs == [result["install_dir"]]

    failed_manager = _fixture_runtime_manager(
        tmp_path / "failed-runtime",
        manifest_path=tmp_path / "missing.json",
        offline=True,
    )
    failed_calls: list[None] = []
    monkeypatch.setattr(
        failed_manager,
        "_clean_after_successful_install",
        lambda: failed_calls.append(None),
    )

    failed = failed_manager.ensure()

    assert failed["ok"] is False
    assert failed_calls == []


def test_ensure_keeps_published_success_when_cleanup_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="cleanup-failure",
        version="1.0.0",
    )
    manager = _fixture_runtime_manager(tmp_path / "runtime", manifest_path=manifest_path)

    def fail_cleanup() -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(manager, "_clean_after_successful_install", fail_cleanup)

    result = manager.ensure()

    assert result["ok"] is True
    assert result["changed"] is True
    assert Path(result["path"]).is_file()
    pointer = json.loads((manager.runtime_dir / "current.json").read_text(encoding="utf-8"))
    assert pointer["install_dir"] == result["install_dir"]


def _age_path(path: Path) -> None:
    stamp = time.time() - 3600
    os.utime(path, (stamp, stamp))


def _archive_provenance(manager: ManagedRuntimeManager) -> set[tuple[str, str]]:
    payload = json.loads(manager._archive_provenance_path.read_text(encoding="utf-8"))
    return {(entry["name"], entry["sha256"]) for entry in payload["archives"]}


def _archive_unlink_failure(archive_path: Path, real_unlink):
    def _refuse(path, *args, **kwargs):
        requested = Path(path)
        matches = (
            requested == Path(archive_path.name)
            if kwargs.get("dir_fd") is not None
            else requested == archive_path
        )
        if matches:
            raise OSError("archive is in use")
        return real_unlink(path, *args, **kwargs)

    return _refuse


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_subclass_relative_runtime_directory_persists_an_admissible_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    monkeypatch.chdir(tmp_path)
    relative_runtime_dir = Path("relative-runtimes") / runtime_kind
    manager = _subclass_runtime_manager(
        tmp_path,
        runtime_kind,
        manifest,
        monkeypatch,
        runtime_dir=relative_runtime_dir,
    )

    installed = manager.ensure()

    assert installed["ok"] is True
    assert manager.runtime_dir == tmp_path / relative_runtime_dir
    assert Path(installed["install_dir"]).is_absolute()
    assert manager.status()["path"] == installed["path"]
    assert _resolve_subclass_runtime(manager, runtime_kind) == Path(installed["path"])


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_installed_subclass_status_and_resolution_survive_unavailable_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True
    installed_path = Path(installed["path"])
    pointer_path = manager.runtime_dir / "current.json"
    metadata_path = Path(installed["install_dir"]) / manager.spec.metadata_filename
    pointer_before = pointer_path.read_bytes()
    metadata_before = metadata_path.read_bytes()

    _change_unrelated_platform(manifest, runtime_kind)
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("disk-local inspection accessed an archive"),
    )
    monkeypatch.setattr(
        manager,
        "_write_current_pointer",
        lambda *_args: pytest.fail("disk-local inspection rewrote the active pointer"),
    )

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert reused["path"] == str(installed_path)
    assert reused["install_dir"] == installed["install_dir"]
    assert pointer_path.read_bytes() == pointer_before
    assert metadata_path.read_bytes() == metadata_before

    manifest.unlink()
    status = manager.status()

    assert status["installed"] is True
    assert status["version"] == {"git": "2.55.0", "memory": "1.2.3", "model-hub": "v7.2.95"}[
        runtime_kind
    ]
    assert status["selected_version"] is None
    assert status["matches_manifest"] is None
    assert status["path"] == str(installed_path)
    assert status["install_dir"] == installed["install_dir"]
    assert status["reason"] is None
    assert _resolve_subclass_runtime(manager, runtime_kind) == installed_path
    assert pointer_path.read_bytes() == pointer_before
    assert metadata_path.read_bytes() == metadata_before


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_subclass_operational_resolution_uses_disk_snapshot_when_manifest_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True
    installed_path = Path(installed["path"])
    manifest.unlink()

    status = manager.status()
    resolved = _resolve_subclass_runtime(manager, runtime_kind)

    assert (status["installed"], status["path"], resolved) == (
        True,
        str(installed_path),
        installed_path,
    )


def test_git_resolves_a_runtime_version_accepted_by_the_installer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _archive, manifest = _write_subclass_runtime_fixture(
        tmp_path,
        "git",
        runtime_version="2.55.0+avibe.1",
    )
    manager = _subclass_runtime_manager(tmp_path, "git", manifest, monkeypatch)

    installed = manager.ensure()
    manifest.unlink()

    assert installed["ok"] is True
    assert manager.status()["version"] == "2.55.0+avibe.1"
    assert manager.resolve_git_path() == Path(installed["path"])


@pytest.mark.parametrize("bin_path", ["python", "usr/bin/python"])
def test_memory_status_uses_the_recorded_install_dir_for_any_safe_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bin_path: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(
        tmp_path,
        "memory",
        bin_path=bin_path,
    )
    manager = _subclass_runtime_manager(tmp_path, "memory", manifest, monkeypatch)

    installed = manager.ensure()
    status = manager.status()

    assert installed["ok"] is True
    assert status["installed"] is True
    assert status["matches_manifest"] is True
    assert status["path"] == installed["path"]
    assert status["install_dir"] == installed["install_dir"]


def test_shared_status_retries_when_current_pointer_switches_during_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, "git")
    manager = _subclass_runtime_manager(tmp_path, "git", manifest, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True
    manifest.unlink()

    pointer_path = manager.runtime_dir / "current.json"
    old_pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    new_install_dir = manager.runtime_dir / "versions" / "concurrent-replacement"
    old_install_dir = Path(installed["install_dir"])
    shutil.copytree(old_install_dir, new_install_dir)
    metadata_path = new_install_dir / manager.spec.metadata_filename
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["runtime_version"] = "2.56.0"
    managed_runtime.write_json_atomic(metadata_path, metadata)
    new_pointer = {
        **old_pointer,
        "runtime_version": "2.56.0",
        "install_dir": str(new_install_dir),
    }

    original_resolve = manager.resolve_binary
    resolve_calls = 0

    def switch_pointer_after_first_resolution() -> Path | None:
        nonlocal resolve_calls
        binary = original_resolve()
        resolve_calls += 1
        if resolve_calls == 1:
            managed_runtime.write_json_atomic(pointer_path, new_pointer)
        return binary

    monkeypatch.setattr(manager, "resolve_binary", switch_pointer_after_first_resolution)

    status = manager.status()

    expected_binary = new_install_dir / manager.spec.default_bin_path
    assert resolve_calls == 2
    assert status["installed"] is True
    assert status["version"] == "2.56.0"
    assert status["path"] == str(expected_binary)
    assert status["install_dir"] == str(new_install_dir)


@pytest.mark.parametrize("runtime_kind", ["git", "model-hub"])
def test_subclass_derives_released_missing_bin_path_from_safe_spec_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True
    installed_path = Path(installed["path"])
    pointer_path = manager.runtime_dir / "current.json"
    metadata_path = Path(installed["install_dir"]) / manager.spec.metadata_filename
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    pointer.pop("bin_path")
    metadata.pop("bin_path")
    managed_runtime.write_json_atomic(pointer_path, pointer)
    managed_runtime.write_json_atomic(metadata_path, metadata)
    pointer_before = pointer_path.read_bytes()
    metadata_before = metadata_path.read_bytes()
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("safe default entrypoint accessed an archive"),
    )
    monkeypatch.setattr(
        manager,
        "_write_current_pointer",
        lambda *_args: pytest.fail("safe default entrypoint rewrote the pointer"),
    )

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert reused["path"] == str(installed_path)
    manifest.unlink()
    status = manager.status()
    assert status["installed"] is True
    assert status["path"] == str(installed_path)
    assert _resolve_subclass_runtime(manager, runtime_kind) == installed_path
    assert pointer_path.read_bytes() == pointer_before
    assert metadata_path.read_bytes() == metadata_before


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_subclass_status_rejects_an_unreadable_installed_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True

    def unreadable_binary(_path: Path) -> str:
        raise OSError("binary became unreadable")

    monkeypatch.setattr(managed_runtime, "file_sha256", unreadable_binary)

    status = manager.status()

    assert status["installed"] is False
    assert status["status"] == "error"
    assert status["reason"] is not None
    assert _resolve_subclass_runtime(manager, runtime_kind) is None


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
@pytest.mark.parametrize("state_file", ["pointer", "metadata"])
def test_subclass_projects_deep_installed_json_as_an_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
    state_file: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True
    state_path = (
        manager.runtime_dir / "current.json"
        if state_file == "pointer"
        else Path(installed["install_dir"]) / manager.spec.metadata_filename
    )
    state_path.write_text(
        "[" * 2_000 + "0" + "]" * 2_000,
        encoding="utf-8",
    )

    status = manager.status()

    assert status["installed"] is False
    assert status["status"] == "error"
    assert _resolve_subclass_runtime(manager, runtime_kind) is None


@pytest.mark.parametrize("runtime_kind", ["git", "model-hub"])
def test_shared_reuse_repairs_pointer_from_persisted_metadata_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True
    pointer_path = manager.runtime_dir / "current.json"
    metadata_path = Path(installed["install_dir"]) / manager.spec.metadata_filename
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["manifest_sha256"] = "f" * 64
    managed_runtime.write_json_atomic(pointer_path, pointer)
    metadata_before = metadata_path.read_bytes()
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("pointer repair accessed an archive"),
    )

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    repaired = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert repaired["manifest_sha256"] == metadata["manifest_sha256"]
    assert metadata_path.read_bytes() == metadata_before
    assert _resolve_subclass_runtime(manager, runtime_kind) == Path(installed["path"])


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_invalid_manifest_keeps_disk_resolution_but_blocks_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True
    installed_path = Path(installed["path"])
    manifest.write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("invalid manifest repair accessed an archive"),
    )

    status = manager.status()
    resolved = _resolve_subclass_runtime(manager, runtime_kind)
    repair = manager.ensure()

    assert status["installed"] is True
    assert status["path"] == str(installed_path)
    assert resolved == installed_path
    assert repair["ok"] is False
    assert str(repair["reason"]).endswith("manifest_invalid")


def test_memory_reuse_refreshes_changed_manifest_contract_through_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    manager = _subclass_runtime_manager(tmp_path, "memory", manifest_path, monkeypatch)
    assert isinstance(manager, MemoryArtifactManager)
    installed = manager.ensure()
    assert installed["ok"] is True

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["provider_root_format"] = "everos-2.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    updated_manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    selected_status = manager.status()

    assert selected_status["installed"] is True
    assert selected_status["path"] == installed["path"]
    assert selected_status["matches_manifest"] is False

    activations: list[str] = []

    def activate(candidate, root_state, commit, _rollback) -> None:
        activations.append(candidate.provider_root_format)
        assert root_state is not None
        assert root_state.exists is False
        commit()

    manager.set_activation_coordinator(activate)
    sync_admissions: list[tuple[int, tuple[str, ...], str, str] | None] = []

    def admit_sync(
        _binary: Path,
        contract: tuple[int, tuple[str, ...], str, str] | None,
    ) -> bool:
        sync_admissions.append(contract)
        return True

    monkeypatch.setattr(manager, "_admit_sync_contract", admit_sync)
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("Memory contract refresh accessed an archive"),
    )

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert reused["path"] == installed["path"]
    assert activations == ["everos-2.0"]
    active = manager._active_pointer()
    assert active is not None
    assert active["provider_root_format"] == "everos-2.0"
    assert active["compatible_provider_root_formats"] == []
    assert active["artifact_fingerprint"] == updated_manifest_digest[:16]
    assert sync_admissions == []
    assert manager.status()["matches_manifest"] is True


def test_memory_selected_contract_failure_preserves_admitted_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    manager = _subclass_runtime_manager(tmp_path, "memory", manifest_path, monkeypatch)
    assert isinstance(manager, MemoryArtifactManager)
    installed = manager.ensure()
    assert installed["ok"] is True
    pointer_path = manager.runtime_dir / "current.json"
    pointer_before = pointer_path.read_bytes()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["provider_root_format"] = "everos-next"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(manager, "_prepare_binary", lambda _binary, **_kwargs: {"ok": False})
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("failed Memory re-admission accessed an archive"),
    )

    failed = manager.ensure()

    assert failed["ok"] is False
    assert pointer_path.read_bytes() == pointer_before
    assert manager.status()["installed"] is True
    assert manager.resolve_python() == Path(installed["path"])


def test_memory_preparation_timeout_reclaims_staging_and_persists_latest_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    runtime_dir = tmp_path / "memory-runtime"
    manager = _subclass_runtime_manager(
        tmp_path,
        "memory",
        manifest_path,
        monkeypatch,
        runtime_dir=runtime_dir,
    )
    monkeypatch.setattr(
        manager,
        "_prepare_binary",
        lambda _binary, **_kwargs: {
            "ok": False,
            "reason": COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON,
        },
    )

    failed = manager.ensure()

    assert failed["ok"] is False
    assert failed["reason"] == COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON
    assert not any(path.name.startswith("install-") for path in runtime_dir.iterdir())
    failure_path = runtime_dir / "last-install-failure.json"
    assert json.loads(failure_path.read_text(encoding="utf-8")) == {
        "reason": COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON,
        "status": "error",
    }

    restarted = _subclass_runtime_manager(
        tmp_path,
        "memory",
        manifest_path,
        monkeypatch,
        runtime_dir=runtime_dir,
    )
    restarted_status = restarted.status()
    assert restarted_status["installed"] is False
    assert restarted_status["status"] == "error"
    assert restarted_status["reason"] == COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON

    succeeded = restarted.ensure()
    assert succeeded["ok"] is True
    assert failure_path.exists() is False
    assert restarted.status()["reason"] is None


def test_memory_legacy_pointer_admission_failure_survives_fresh_manager_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    runtime_dir = tmp_path / "memory-runtime"
    manager = _subclass_runtime_manager(
        tmp_path,
        "memory",
        manifest_path,
        monkeypatch,
        runtime_dir=runtime_dir,
    )
    assert isinstance(manager, MemoryArtifactManager)
    assert manager.ensure()["ok"] is True
    pointer = manager._active_pointer()
    assert pointer is not None
    pointer.pop("admission_revision")
    pointer.pop("admission_ok")
    manager._restore_current_pointer(pointer)
    monkeypatch.setattr(
        manager,
        "_prepare_binary",
        lambda _binary, **_kwargs: {
            "ok": False,
            "reason": COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON,
        },
    )

    assert manager.resolve_python() is None

    restarted = _subclass_runtime_manager(
        tmp_path,
        "memory",
        manifest_path,
        monkeypatch,
        runtime_dir=runtime_dir,
    )
    restarted_status = restarted.status()
    assert restarted_status["installed"] is False
    assert restarted_status["status"] == "error"
    assert restarted_status["reason"] == COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON

    (Path(pointer["install_dir"]) / pointer["bin_path"]).unlink()
    corrupted_status = restarted.status()
    assert corrupted_status["installed"] is False
    assert corrupted_status["reason"] == "memory_runtime_install_failed"


def test_memory_scrubber_timeout_keeps_its_preparation_stage_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        manifest_path=tmp_path / "missing.json",
        provider_root=tmp_path / "provider-root",
    )

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(memory_artifact.subprocess, "run", timeout)

    assert manager._admit_error_scrubbers(tmp_path / "runtime" / "bin" / "python") == (
        "memory_runtime_preparation_scrubber_timeout"
    )


def test_memory_sync_contract_failure_keeps_its_preparation_stage_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        manifest_path=tmp_path / "missing.json",
        provider_root=tmp_path / "provider-root",
    )
    monkeypatch.setattr(
        memory_artifact,
        "run_cold_artifact_admission",
        lambda _binary: SimpleNamespace(ok=True, reason=None, duration_ms=1),
    )
    monkeypatch.setattr(manager, "_admit_error_scrubbers", lambda _binary: None)
    monkeypatch.setattr(manager, "_admit_sync_contract", lambda _binary, _expected: False)

    result = manager._prepare_binary(
        tmp_path / "runtime" / "bin" / "python",
        sync_contract=(1, ("write",), "a" * 64, "b" * 64),
    )

    assert result == {
        "ok": False,
        "reason": "memory_runtime_preparation_sync_contract_failed",
    }


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"[]",
        b'{"reason":"memory_runtime_preparation_import_timeout"}',
        b'{"status":"failed","reason":"memory_runtime_preparation_import_timeout"}',
        b'{"status":"error","reason":42}',
        b"x" * (4 * 1024 + 1),
    ],
)
def test_memory_latest_install_failure_safely_ignores_malformed_or_older_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    runtime_dir = tmp_path / "memory-runtime"
    runtime_dir.mkdir(mode=0o700)
    failure_path = runtime_dir / "last-install-failure.json"
    failure_path.write_bytes(content)
    failure_path.chmod(0o600)
    manager = _subclass_runtime_manager(
        tmp_path,
        "memory",
        manifest_path,
        monkeypatch,
        runtime_dir=runtime_dir,
    )

    status_payload = manager.status()

    assert status_payload["installed"] is False
    assert status_payload["status"] == "missing"
    assert status_payload["reason"] is None


def test_memory_latest_install_failure_rejects_symlink_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    runtime_dir = tmp_path / "memory-runtime"
    runtime_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "status": "error",
                "reason": COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON,
            }
        ),
        encoding="utf-8",
    )
    (runtime_dir / "last-install-failure.json").symlink_to(outside)
    manager = _subclass_runtime_manager(
        tmp_path,
        "memory",
        manifest_path,
        monkeypatch,
        runtime_dir=runtime_dir,
    )

    status_payload = manager.status()

    assert status_payload["status"] == "missing"
    assert status_payload["reason"] is None
    assert outside.is_file()


def test_memory_reuse_clears_latest_install_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    manager = _subclass_runtime_manager(tmp_path, "memory", manifest_path, monkeypatch)
    assert manager.ensure()["ok"] is True
    failure_path = manager.runtime_dir / "last-install-failure.json"
    manager._write_latest_install_failure(COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON)
    assert failure_path.is_file()

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert failure_path.exists() is False


def test_memory_skipped_install_does_not_persist_terminal_failure(
    tmp_path: Path,
) -> None:
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        manifest_path=tmp_path / "missing.json",
        provider_root=tmp_path / "provider-root",
    )

    result = manager._failure(
        manager._reason("install_already_running"),
        skipped=True,
    )

    assert result["skipped"] is True
    assert (manager.runtime_dir / "last-install-failure.json").exists() is False


def test_memory_install_failure_persistence_bounds_unsafe_reason(
    tmp_path: Path,
) -> None:
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "runtime",
        manifest_path=tmp_path / "missing.json",
        provider_root=tmp_path / "provider-root",
    )

    manager._failure("unsafe reason " + "x" * 1024)

    persisted = json.loads(
        (manager.runtime_dir / "last-install-failure.json").read_text(encoding="utf-8")
    )
    assert persisted == {
        "status": "error",
        "reason": "memory_runtime_preparation_failed",
    }


def test_memory_force_pointer_failure_preserves_active_install_and_retry_repairs_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    manager = _subclass_runtime_manager(tmp_path, "memory", manifest_path, monkeypatch)
    assert isinstance(manager, MemoryArtifactManager)
    installed = manager.ensure()
    assert installed["ok"] is True
    pointer_path = manager.runtime_dir / "current.json"
    metadata_path = Path(installed["install_dir"]) / manager.spec.metadata_filename
    metadata_before = metadata_path.read_bytes()
    install_dirs_before = {
        path.parent for path in manager.runtime_dir.rglob(manager.spec.metadata_filename)
    }
    original_pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["provider_root_format"] = "everos-2.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    real_write_pointer = manager._write_memory_current_pointer

    def fail_pointer_write(*_args, **_kwargs) -> None:
        raise MemoryRuntimeActivationError("pointer write failed")

    monkeypatch.setattr(manager, "_write_memory_current_pointer", fail_pointer_write)

    failed = manager.ensure(force=True)

    assert failed["ok"] is False
    assert metadata_path.read_bytes() == metadata_before
    assert json.loads(pointer_path.read_text(encoding="utf-8")) == original_pointer
    assert manager.resolve_python() == Path(installed["path"])
    assert {
        path.parent for path in manager.runtime_dir.rglob(manager.spec.metadata_filename)
    } == install_dirs_before

    monkeypatch.setattr(manager, "_write_memory_current_pointer", real_write_pointer)
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("pointer retry accessed an archive"),
    )

    retried = manager.ensure()

    assert retried["ok"] is True
    assert retried["changed"] is False
    repaired_pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    repaired_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert repaired_pointer["manifest_sha256"] == repaired_metadata["manifest_sha256"]
    assert repaired_pointer["manifest_sha256"] != original_pointer["manifest_sha256"]
    assert repaired_pointer["install_dir"] == installed["install_dir"]


def test_memory_incompatible_provider_root_failure_is_repair_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    manager = _subclass_runtime_manager(tmp_path, "memory", manifest_path, monkeypatch)
    assert isinstance(manager, MemoryArtifactManager)
    assert manager.ensure()["ok"] is True

    def reject_incompatible_root(_candidate) -> None:
        raise ProviderRootError("memory provider root format is incompatible")

    monkeypatch.setattr(manager._provider_root, "inspect", reject_incompatible_root)

    failed = manager.ensure(force=True)

    assert failed["ok"] is False
    assert failed["reason"] == "memory_local_data_unusable"


def test_memory_force_repair_rollback_preserves_the_active_install_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, "memory")
    manager = _subclass_runtime_manager(tmp_path, "memory", manifest_path, monkeypatch)
    assert isinstance(manager, MemoryArtifactManager)
    installed = manager.ensure()
    assert installed["ok"] is True
    active_dir = Path(installed["install_dir"])
    pointer_path = manager.runtime_dir / "current.json"
    metadata_path = active_dir / manager.spec.metadata_filename
    pointer_before = pointer_path.read_bytes()
    metadata_before = metadata_path.read_bytes()
    binary_before = Path(installed["path"]).read_bytes()
    install_dirs_before = {path.parent for path in manager.runtime_dir.rglob(manager.spec.metadata_filename)}
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["source"] = "fixture-force-repair"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    def reject_candidate(_candidate, _root_state, commit, rollback) -> None:
        commit()
        candidate = manager._active_pointer()
        assert candidate is not None
        assert candidate["install_dir"] != str(active_dir)
        rollback()
        raise MemoryRuntimeActivationError("candidate rejected")

    manager.set_activation_coordinator(reject_candidate)

    failed = manager.ensure(force=True)

    assert failed["ok"] is False
    assert pointer_path.read_bytes() == pointer_before
    assert metadata_path.read_bytes() == metadata_before
    assert Path(installed["path"]).read_bytes() == binary_before
    assert manager.resolve_python() == Path(installed["path"])
    assert {path.parent for path in manager.runtime_dir.rglob(manager.spec.metadata_filename)} == install_dirs_before

    def accept_candidate(_candidate, _root_state, commit, _rollback) -> None:
        commit()

    manager.set_activation_coordinator(accept_candidate)
    repaired = manager.ensure(force=True)
    assert repaired["ok"] is True
    assert repaired["changed"] is True
    assert repaired["install_dir"] != str(active_dir)
    assert active_dir.is_dir()
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("successful force reuse accessed an archive"),
    )

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert reused["install_dir"] == repaired["install_dir"]


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_existing_subclass_adopts_released_manifest_digest_layout_without_write_or_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest_path, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True

    manifest = manager._load_manifest(allow_network=False)
    assert manifest is not None
    archive = manager._manifest_archive_for_platform(manifest)
    assert archive is not None
    install_dir = Path(installed["install_dir"])
    released_fingerprint = hashlib.sha256(
        f"{manifest.digest}:{archive.sha256}".encode("utf-8")
    ).hexdigest()[:16]
    released_install_dir = install_dir.parent / released_fingerprint
    if released_install_dir != install_dir:
        install_dir.rename(released_install_dir)
    pointer_path = manager.runtime_dir / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["install_dir"] = str(released_install_dir)
    pointer.pop("binary_sha256", None)
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    released_binary = released_install_dir / archive.bin_path
    metadata_path = released_install_dir / manager.spec.metadata_filename
    pointer_before = pointer_path.read_bytes()
    metadata_before = metadata_path.read_bytes()
    install_mtime_before = released_install_dir.stat().st_mtime_ns

    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("released install adoption accessed an archive"),
    )
    monkeypatch.setattr(
        manager,
        "_write_current_pointer",
        lambda *_args: pytest.fail("released install adoption rewrote the active pointer"),
    )

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert reused["install_dir"] == str(released_install_dir)
    assert Path(reused["path"]) == released_binary
    assert pointer_path.read_bytes() == pointer_before
    assert metadata_path.read_bytes() == metadata_before
    assert released_install_dir.stat().st_mtime_ns == install_mtime_before

    manifest_path.unlink()
    status = manager.status()
    assert status["installed"] is True
    assert status["path"] == str(released_binary)
    assert _resolve_subclass_runtime(manager, runtime_kind) == released_binary
    assert pointer_path.read_bytes() == pointer_before
    assert metadata_path.read_bytes() == metadata_before


def test_clean_dry_run_is_read_only_and_creates_no_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    runtime_dir = tmp_path / "git-runtime"
    versions = runtime_dir / "versions" / "v1" / "linux-x64" / "aaa"
    versions.mkdir(parents=True)
    manager = GitRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )
    (versions / manager.spec.metadata_filename).write_text("{}", encoding="utf-8")

    result = manager.clean(dry_run=True)

    assert result["ok"] is True
    assert not (runtime_dir / ".install.lock").exists()


def _retention_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
    *,
    current_is_newest: bool,
) -> tuple[ManagedRuntimeManager, Path, list[Path]]:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest_path = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    manager = _subclass_runtime_manager(tmp_path, runtime_kind, manifest_path, monkeypatch)
    installed = manager.ensure()
    assert installed["ok"] is True
    current = Path(installed["install_dir"])
    previous = [current.with_name(f"previous-{index}") for index in range(3)]
    for path in previous:
        shutil.copytree(current, path)

    current_mtime = 400 if current_is_newest else 100
    previous_mtimes = (300, 200, 100) if current_is_newest else (400, 300, 200)
    os.utime(current, (current_mtime, current_mtime))
    for path, mtime in zip(previous, previous_mtimes):
        os.utime(path, (mtime, mtime))
    return manager, current, previous


@pytest.mark.parametrize("keep_previous", [0, 1, 2])
@pytest.mark.parametrize("current_is_newest", [True, False])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_clean_retains_current_plus_requested_previous_regardless_of_mtime_rank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep_previous: int,
    current_is_newest: bool,
    dry_run: bool,
    runtime_kind: str,
) -> None:
    manager, current, previous = _retention_fixture(
        tmp_path,
        monkeypatch,
        runtime_kind,
        current_is_newest=current_is_newest,
    )
    expected_removed = set(previous[keep_previous:])

    result = manager.clean(keep_previous=keep_previous, dry_run=dry_run)

    assert result["ok"] is True
    assert {Path(path) for path in result["removed"]} == expected_removed
    assert current.is_dir()
    expected_remaining = set(previous) if dry_run else set(previous[:keep_previous])
    assert {path for path in previous if path.is_dir()} == expected_remaining


def test_clean_retention_count_is_bounded_by_available_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, current, previous = _retention_fixture(
        tmp_path,
        monkeypatch,
        "git",
        current_is_newest=False,
    )
    shutil.rmtree(previous[1])
    shutil.rmtree(previous[2])

    result = manager.clean(keep_previous=2)

    assert result["ok"] is True
    assert result["reason"] is None
    assert result["removed"] == []
    assert current.is_dir()
    assert previous[0].is_dir()


def test_clean_reclaims_name_addressed_archives_without_cross_lineage_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    packaged_manifest = tmp_path / "packaged-manifest.json"
    custom_manifest = tmp_path / "custom-manifest.json"
    monkeypatch.setattr(managed_runtime.package_resources, "files", lambda _package: tmp_path)
    packaged_manager = FixtureRuntimeManager(
        spec=ManagedRuntimeSpec(
            runtime_id="fixture",
            manifest_resource=packaged_manifest.name,
            version_field="runtime_version",
            default_bin_path="bin/runtime",
        ),
        runtime_dir=runtime_dir,
    )
    old_packaged, old_packaged_archive = _install_fixture_runtime_release(
        packaged_manager,
        tmp_path,
        packaged_manifest,
        label="packaged-old",
        version="packaged-old",
    )
    head_packaged, head_packaged_archive = _install_fixture_runtime_release(
        packaged_manager,
        tmp_path,
        packaged_manifest,
        label="packaged-head",
        version="packaged-head",
    )
    custom_manager = _fixture_runtime_manager(runtime_dir, manifest_path=custom_manifest)
    current_custom, current_custom_archive = _install_fixture_runtime_release(
        custom_manager,
        tmp_path,
        custom_manifest,
        label="custom-current",
        version="custom-current",
    )
    for path, mtime in (
        (old_packaged, 100),
        (head_packaged, 200),
        (current_custom, 300),
    ):
        os.utime(path, (mtime, mtime))
    for archive_path in (
        old_packaged_archive,
        head_packaged_archive,
        current_custom_archive,
    ):
        _age_path(archive_path)

    preview = custom_manager.clean(keep_previous=0, dry_run=True)

    assert preview["ok"] is True
    assert preview["reason"] is None
    assert preview["removed"] == [str(old_packaged)]
    assert preview["archives"] == {
        "outcome": "partial",
        "removed_count": 0,
        "removed_bytes": 0,
        "candidate_count": 1,
        "candidate_bytes": old_packaged_archive.stat().st_size,
        "failed_count": 0,
        "skipped_reason": None,
    }

    result = custom_manager.clean(keep_previous=0)

    assert result["ok"] is True
    assert result["reason"] is None
    assert result["removed"] == [str(old_packaged)]
    assert not old_packaged.exists()
    assert not old_packaged_archive.exists()
    assert head_packaged.is_dir() and head_packaged_archive.is_file()
    assert current_custom.is_dir() and current_custom_archive.is_file()
    assert result["archives"]["outcome"] == "cleaned"
    assert result["archives"]["candidate_count"] == 1
    assert result["archives"]["removed_count"] == 1
    assert result["archives"]["removed_bytes"] == result["archives"]["candidate_bytes"]


def test_clean_preserves_remote_manifest_cache_for_offline_resolution(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "remote-manifest.json"
    source_archive = _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    manifest_url = manifest_path.as_uri()
    online = _fixture_runtime_manager(runtime_dir, manifest_url=manifest_url)
    installed = online.ensure()
    assert installed["ok"] is True
    cached_archive = runtime_dir / "downloads" / source_archive.name
    manifest_caches = list((runtime_dir / "downloads").glob("manifest-*.json"))
    assert len(manifest_caches) == 1
    _age_path(cached_archive)
    _age_path(manifest_caches[0])

    result = online.clean(keep_previous=0)

    assert result["ok"] is True
    assert cached_archive.is_file()
    assert manifest_caches[0].is_file()
    manifest_path.unlink()
    source_archive.unlink()
    offline = _fixture_runtime_manager(runtime_dir, manifest_url=manifest_url, offline=True)
    reused = offline.ensure()
    assert reused["ok"] is True
    assert reused["changed"] is False
    assert Path(reused["install_dir"]) == Path(installed["install_dir"])


def test_archive_probe_fetches_remote_manifest_without_mutating_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "remote-manifest.json"
    _write_fixture_runtime_release(
        tmp_path,
        manifest_path,
        label="diagnostic",
        version="1.0.0",
    )
    manifest_url = "https://example.test/runtime-manifest.json"
    manager = _fixture_runtime_manager(
        tmp_path / "runtime",
        manifest_url=manifest_url,
    )
    cached_manifest = manager._remote_manifest_cache_path()
    cached_manifest.parent.mkdir(parents=True)
    cached_manifest.write_bytes(b"existing cached manifest")
    monkeypatch.setattr(
        managed_runtime,
        "fetch_bytes",
        lambda url, **_kwargs: (
            manifest_path.read_bytes()
            if url == manifest_url
            else pytest.fail(f"unexpected fetch: {url}")
        ),
    )
    monkeypatch.setattr(
        managed_runtime,
        "write_atomic",
        lambda *_args, **_kwargs: pytest.fail("diagnostic load wrote the manifest cache"),
    )
    monkeypatch.setattr(
        managed_runtime,
        "probe_url",
        lambda *_args, **_kwargs: {"ok": True, "checked": True},
    )

    result = manager.probe_archive_reachability()

    assert result == {"ok": True, "checked": True}
    assert cached_manifest.read_bytes() == b"existing cached manifest"


def test_clean_archive_candidates_require_known_shape_maturity_and_unprotected_digest(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    installed: dict[str, tuple[Path, Path]] = {}
    releases = (
        ("symlink", "symlink", None),
        ("directory", "directory", None),
        ("stale", "stale", None),
        ("recent", "recent", None),
        ("tmp", "tmp", "fixture-known.tmp"),
        ("newline", "newline", "fixture-known.tgz\n"),
        ("current", "current", None),
    )
    for index, (label, version, archive_name) in enumerate(releases, start=1):
        installed[label] = _install_fixture_runtime_release(
            manager,
            tmp_path,
            manifest_path,
            label=label,
            version=version,
            archive_name=archive_name,
        )
        install_dir, _cache_path = installed[label]
        os.utime(install_dir, (index * 100, index * 100))

    outside = tmp_path / "outside-archive"
    outside.write_bytes(b"outside")
    symlink_cache = installed["symlink"][1]
    symlink_cache.unlink()
    symlink_cache.symlink_to(outside)
    directory_cache = installed["directory"][1]
    directory_cache.unlink()
    directory_cache.mkdir()
    stale_cache = installed["stale"][1]
    _age_path(stale_cache)
    tmp_cache = installed["tmp"][1]
    _age_path(tmp_cache)
    newline_cache = installed["newline"][1]
    _age_path(newline_cache)
    current_cache = installed["current"][1]
    _age_path(current_cache)
    unknown_cache = runtime_dir / "downloads" / "unknown-archive.tar.gz"
    unknown_cache.write_bytes(b"unknown")
    _age_path(unknown_cache)

    result = manager.clean(keep_previous=0)

    assert result["ok"] is True
    assert result["archives"]["candidate_count"] == 1
    assert result["archives"]["removed_count"] == 1
    assert not stale_cache.exists()
    assert installed["recent"][1].is_file()
    assert symlink_cache.is_symlink() and outside.read_bytes() == b"outside"
    assert directory_cache.is_dir()
    assert tmp_cache.is_file()
    assert newline_cache.is_file()
    assert unknown_cache.is_file()
    assert current_cache.is_file()


def test_clean_retries_recent_archive_from_durable_provenance(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
    )
    current, _current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    stale_digest = hashlib.sha256(stale_archive.read_bytes()).hexdigest()

    first = manager.clean(keep_previous=0)

    assert first["ok"] is True
    assert not stale.exists() and stale_archive.is_file()
    assert _archive_provenance(manager) == {(stale_archive.name, stale_digest)}

    _age_path(stale_archive)
    second = manager.clean(keep_previous=0)

    assert second["ok"] is True
    assert second["archives"]["candidate_count"] == 1
    assert second["archives"]["removed_count"] == 1
    assert not stale_archive.exists()
    assert _archive_provenance(manager) == set()


def test_clean_retries_parser_valid_long_archive_basename_from_provenance(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    archive_name = f"{'a' * 129} runtime-版本@%.tar.gz"
    assert len(archive_name.encode("utf-8")) > 128
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
        archive_name=archive_name,
    )
    current, _current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    stale_digest = hashlib.sha256(stale_archive.read_bytes()).hexdigest()

    first = manager.clean(keep_previous=0)

    assert first["ok"] is True
    assert not stale.exists() and stale_archive.is_file()
    assert _archive_provenance(manager) == {(archive_name, stale_digest)}

    _age_path(stale_archive)
    second = manager.clean(keep_previous=0)

    assert second["ok"] is True
    assert second["archives"]["candidate_count"] == 1
    assert second["archives"]["removed_count"] == 1
    assert not stale_archive.exists()
    assert _archive_provenance(manager) == set()


def test_clean_retries_archive_after_transient_unlink_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
    )
    current, _current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    _age_path(stale_archive)
    stale_digest = hashlib.sha256(stale_archive.read_bytes()).hexdigest()
    real_unlink = os.unlink

    monkeypatch.setattr(os, "unlink", _archive_unlink_failure(stale_archive, real_unlink))
    first = manager.clean(keep_previous=0)

    assert first["ok"] is False
    assert not stale.exists() and stale_archive.is_file()
    assert _archive_provenance(manager) == {(stale_archive.name, stale_digest)}

    monkeypatch.setattr(os, "unlink", real_unlink)
    second = manager.clean(keep_previous=0)

    assert second["ok"] is True
    assert second["archives"]["candidate_count"] == 1
    assert second["archives"]["removed_count"] == 1
    assert not stale_archive.exists()
    assert _archive_provenance(manager) == set()


def test_clean_fails_closed_when_archive_provenance_cannot_be_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
    )
    current, current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    staging_dir = runtime_dir / "install-pending"
    staging_dir.mkdir()
    monkeypatch.setattr(
        manager,
        "_write_archive_provenance",
        lambda _provenance: (_ for _ in ()).throw(OSError("disk is read-only")),
    )

    result = manager.clean(keep_previous=0)

    assert result["ok"] is False
    assert result["reason"] == "fixture_clean_inspection_failed"
    assert result["removed"] == [str(staging_dir)]
    assert result["archives"]["skipped_reason"] == "archive_inspection_failed"
    assert not staging_dir.exists()
    assert stale.is_dir() and stale_archive.is_file()
    assert current.is_dir() and current_archive.is_file()
    assert not manager._archive_provenance_path.exists()


def test_clean_reclaims_staging_but_rejects_unsafe_archive_provenance(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
    )
    current, current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    staging_dir = runtime_dir / "install-pending"
    staging_dir.mkdir()
    manager._archive_provenance_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_id": "fixture",
                "archives": [{"name": "../outside.tgz", "sha256": "a" * 64}],
            }
        ),
        encoding="utf-8",
    )

    result = manager.clean(keep_previous=0)

    assert result["ok"] is False
    assert result["reason"] == "fixture_clean_inspection_failed"
    assert result["removed"] == [str(staging_dir)]
    assert result["archives"]["skipped_reason"] == "archive_inspection_failed"
    assert not staging_dir.exists()
    assert stale.is_dir() and stale_archive.is_file()
    assert current.is_dir() and current_archive.is_file()


def test_clean_dry_run_does_not_persist_archive_provenance(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
    )
    current, _current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    _age_path(stale_archive)

    preview = manager.clean(keep_previous=0, dry_run=True)

    assert preview["ok"] is True
    assert preview["removed"] == [str(stale)]
    assert preview["archives"]["candidate_count"] == 1
    assert stale.is_dir() and stale_archive.is_file()
    assert not manager._archive_provenance_path.exists()


def test_clean_keeps_recorded_archive_when_bytes_do_not_match_provenance(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
    )
    current, _current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    stale_digest = hashlib.sha256(stale_archive.read_bytes()).hexdigest()
    first = manager.clean(keep_previous=0)
    assert first["ok"] is True
    assert not stale.exists()
    assert _archive_provenance(manager) == {(stale_archive.name, stale_digest)}

    stale_archive.write_bytes(b"replacement bytes")
    _age_path(stale_archive)
    second = manager.clean(keep_previous=0)

    assert second["ok"] is True
    assert second["archives"]["candidate_count"] == 0
    assert stale_archive.read_bytes() == b"replacement bytes"
    assert _archive_provenance(manager) == {(stale_archive.name, stale_digest)}


def test_clean_fails_closed_for_unreadable_retained_archive_metadata(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    rollback, rollback_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="rollback",
        version="rollback",
    )
    current, current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    rollback_metadata = rollback / manager.spec.metadata_filename
    rollback_metadata.write_text("{", encoding="utf-8")
    for archive_path in (rollback_archive, current_archive):
        _age_path(archive_path)

    result = manager.clean(keep_previous=1)

    assert result["ok"] is False
    assert result["reason"] == "fixture_clean_inspection_failed"
    assert result["removed"] == []
    assert result["archives"]["skipped_reason"] == "archive_inspection_failed"
    assert rollback.is_dir() and rollback_archive.is_file()
    assert current.is_dir() and current_archive.is_file()


def test_clean_reclaims_staging_before_unreadable_install_metadata_failure(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    rollback, rollback_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="rollback",
        version="rollback",
    )
    current, current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    (rollback / manager.spec.metadata_filename).write_text("{", encoding="utf-8")
    staging_dir = runtime_dir / "install-pending"
    staging_dir.mkdir()
    for archive_path in (rollback_archive, current_archive):
        _age_path(archive_path)

    result = manager.clean(keep_previous=1)

    assert result["ok"] is False
    assert result["reason"] == "fixture_clean_inspection_failed"
    assert result["removed"] == [str(staging_dir)]
    assert result["archives"]["outcome"] == "skipped"
    assert result["archives"]["skipped_reason"] == "archive_inspection_failed"
    assert not staging_dir.exists()
    assert rollback.is_dir() and rollback_archive.is_file()
    assert current.is_dir() and current_archive.is_file()


def test_clean_does_not_report_an_install_directory_that_removal_did_not_reclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
    )
    current, _current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    _age_path(stale_archive)
    real_rmtree = shutil.rmtree

    def _refuse_one_target(path, *args, **kwargs):
        if Path(path) == stale:
            raise OSError("target is in use")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", _refuse_one_target)
    result = manager.clean(keep_previous=0)

    assert result["ok"] is False
    assert result["reason"] == "fixture_clean_removal_failed"
    assert str(stale) not in result["removed"]
    assert stale.is_dir()
    assert stale_archive.is_file()
    assert result["archives"]["candidate_count"] == 0


def test_clean_archive_removal_failure_uses_shared_failure_and_archive_vocabulary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    manifest_path = tmp_path / "manifest.json"
    manager = _fixture_runtime_manager(runtime_dir, manifest_path=manifest_path)
    stale, stale_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="stale",
        version="stale",
    )
    current, _current_archive = _install_fixture_runtime_release(
        manager,
        tmp_path,
        manifest_path,
        label="current",
        version="current",
    )
    os.utime(stale, (100, 100))
    os.utime(current, (200, 200))
    _age_path(stale_archive)
    real_unlink = os.unlink

    monkeypatch.setattr(os, "unlink", _archive_unlink_failure(stale_archive, real_unlink))
    result = manager.clean(keep_previous=0)

    assert result["ok"] is False
    assert result["reason"] == "fixture_clean_removal_failed"
    assert result["removed"] == [str(stale)]
    assert not stale.exists()
    assert stale_archive.is_file()
    assert result["archives"]["outcome"] == "skipped"
    assert result["archives"]["candidate_count"] == 1
    assert result["archives"]["removed_count"] == 0
    assert result["archives"]["removed_bytes"] == 0
    assert result["archives"]["failed_count"] == 1
    assert result["archives"]["skipped_reason"] == "archive_removal_failed"


@pytest.mark.parametrize(
    "pointer_state",
    ["corrupt", "unreadable", "wrong-root", "absent"],
)
@pytest.mark.parametrize("keep_previous", [0, 1])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_clean_pointer_failure_or_absence_plans_no_install_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer_state: str,
    keep_previous: int,
    dry_run: bool,
    runtime_kind: str,
) -> None:
    manager, current, previous = _retention_fixture(
        tmp_path,
        monkeypatch,
        runtime_kind,
        current_is_newest=False,
    )
    install_dirs = {current, *previous}
    pointer_path = manager.runtime_dir / "current.json"
    original_mode = stat.S_IMODE(pointer_path.stat().st_mode)
    staging_dir: Path | None = None
    if pointer_state == "corrupt":
        pointer_path.write_text("{", encoding="utf-8")
        staging_dir = manager.runtime_dir / "install-pending"
        staging_dir.mkdir()
    elif pointer_state == "unreadable":
        pointer_path.chmod(0)
        staging_dir = manager.runtime_dir / "install-pending"
        staging_dir.mkdir()
    elif pointer_state == "wrong-root":
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        damaged_child = current / "damaged-child"
        damaged_child.mkdir()
        pointer["install_dir"] = str(damaged_child)
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
        staging_dir = manager.runtime_dir / "install-pending"
        staging_dir.mkdir()
    else:
        pointer_path.unlink()
        manifest = manager._load_manifest(allow_network=False)
        assert manifest is not None
        archive = manager._manifest_archive_for_platform(manifest)
        assert archive is not None
        assert all(
            manager._verified_manifest_binary(path, manifest, archive) is not None
            for path in install_dirs
        )

    try:
        result = manager.clean(keep_previous=keep_previous, dry_run=dry_run)
    finally:
        if pointer_state == "unreadable":
            pointer_path.chmod(original_mode)

    assert result["removed"] == []
    assert all(path.is_dir() for path in install_dirs)
    if pointer_state == "absent":
        assert result["ok"] is True
    else:
        assert staging_dir is not None and staging_dir.is_dir()
        assert result["ok"] is False
        assert result["reason"] == manager._reason("clean_inspection_failed")


def test_clean_dry_run_reports_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    manager = GitRuntimeManager(
        runtime_dir=tmp_path / "git-runtime",
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )

    def _boom(*, keep_previous, dry_run=False, removed=None):
        raise OSError("disk unreadable")

    monkeypatch.setattr(manager, "_clean_locked", _boom)
    result = manager.clean(dry_run=True)

    assert result["ok"] is False
    assert result["reason"] == "git_clean_inspection_failed"
    assert "disk unreadable" in result["message"]


def test_clean_reports_inspection_failure_on_real_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    manager = GitRuntimeManager(
        runtime_dir=tmp_path / "git-runtime",
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )

    def _boom(*, keep_previous, dry_run=False, removed=None):
        raise OSError("disk unreadable")

    monkeypatch.setattr(manager, "_clean_locked", _boom)
    result = manager.clean()

    assert result["ok"] is False
    assert result["reason"] == "git_clean_inspection_failed"
    assert "disk unreadable" in result["message"]


def test_clean_dry_run_holds_preview_guard_through_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    runtime_dir = tmp_path / "git-runtime"
    runtime_dir.mkdir()
    (runtime_dir / ".install.lock").write_text("", encoding="utf-8")
    manager = GitRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )
    seen: dict[str, object] = {}
    real_clean_locked = manager._clean_locked

    def _observe(*, keep_previous, dry_run=False, removed=None):
        seen["held_lock"] = getattr(manager, "_preview_held_install_lock", False)
        seen["fd"] = getattr(manager, "_preview_guard_fd", None)
        seen["lock_busy"] = not manager._install_lock.acquire(blocking=False)
        if seen["lock_busy"] is False:
            manager._install_lock.release()
        return real_clean_locked(keep_previous=keep_previous, dry_run=dry_run, removed=removed)

    monkeypatch.setattr(manager, "_clean_locked", _observe)
    result = manager.clean(dry_run=True)

    assert result["ok"] is True
    assert seen["held_lock"] is True
    assert seen["lock_busy"] is True
    assert getattr(manager, "_preview_held_install_lock", False) is False
    assert getattr(manager, "_preview_guard_fd", None) is None
    assert manager._install_lock.acquire(blocking=False)
    manager._install_lock.release()


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_subclass_install_refuses_symlinked_mutation_guard_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    runtime_dir = tmp_path / f"{runtime_kind}-runtime"
    runtime_dir.mkdir()
    victim = tmp_path / f"{runtime_kind}-victim.txt"
    victim.write_text("do not rewrite", encoding="utf-8")
    try:
        (runtime_dir / ".install.lock").symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    manager = _subclass_runtime_manager(
        tmp_path,
        runtime_kind,
        manifest,
        monkeypatch,
        runtime_dir=runtime_dir,
    )

    result = manager.ensure()

    assert victim.read_text(encoding="utf-8") == "do not rewrite"
    assert result["ok"] is False
    assert result["reason"] == manager._reason("install_lock_failed")


def test_real_cleanup_refuses_hardlinked_mutation_guard_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    runtime_dir = tmp_path / "git-runtime"
    runtime_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("do not rewrite", encoding="utf-8")
    try:
        os.link(victim, runtime_dir / ".install.lock")
    except OSError as exc:
        pytest.skip(f"hard-link creation unavailable: {exc}")
    manager = GitRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )

    result = manager.clean()

    assert victim.read_text(encoding="utf-8") == "do not rewrite"
    assert result["ok"] is False
    assert result["reason"] == "git_clean_lock_failed"


@pytest.mark.parametrize(
    ("operation", "expected_reason"),
    [("install", "git_install_lock_failed"), ("clean", "git_clean_lock_failed")],
)
def test_mutation_reports_uninspectable_guard_as_lock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    expected_reason: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, "git")
    runtime_dir = tmp_path / "git-runtime"
    runtime_dir.mkdir()
    lock_path = runtime_dir / ".install.lock"
    lock_path.write_text("", encoding="utf-8")
    manager = GitRuntimeManager(runtime_dir=runtime_dir, manifest_path=manifest)
    real_lstat = Path.lstat

    def _lstat(self):
        if self == lock_path:
            raise OSError("guard unreadable")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _lstat)

    result = manager.ensure() if operation == "install" else manager.clean()

    assert result["ok"] is False
    assert result["reason"] == expected_reason


@pytest.mark.parametrize("runtime_kind", ["git", "memory", "model-hub"])
def test_subclass_preview_classifies_special_guard_as_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: str,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation unavailable")
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, runtime_kind)
    runtime_dir = tmp_path / f"{runtime_kind}-runtime"
    runtime_dir.mkdir()
    os.mkfifo(runtime_dir / ".install.lock")
    manager = _subclass_runtime_manager(
        tmp_path,
        runtime_kind,
        manifest,
        monkeypatch,
        runtime_dir=runtime_dir,
    )

    result = manager.clean(dry_run=True)

    assert result["ok"] is False
    assert result["reason"] == manager._reason("clean_inspection_failed")


def test_preview_classifies_uninspectable_guard_as_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    runtime_dir = tmp_path / "git-runtime"
    runtime_dir.mkdir()
    lock_path = runtime_dir / ".install.lock"
    lock_path.write_text("", encoding="utf-8")
    manager = GitRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )
    real_lstat = Path.lstat

    def _lstat(self):
        if self == lock_path:
            raise OSError("guard unreadable")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _lstat)

    result = manager.clean(dry_run=True)

    assert result["ok"] is False
    assert result["reason"] == "git_clean_inspection_failed"


def test_preview_classifies_special_guard_created_during_planning_as_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation unavailable")
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    runtime_dir = tmp_path / "git-runtime"
    manager = GitRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )

    def _create_special_guard(*, keep_previous, dry_run=False, removed=None):
        runtime_dir.mkdir(parents=True, exist_ok=True)
        os.mkfifo(runtime_dir / ".install.lock")
        return {"ok": True, "removed": []}

    monkeypatch.setattr(manager, "_clean_locked", _create_special_guard)

    result = manager.clean(dry_run=True)

    assert result["ok"] is False
    assert result["reason"] == "git_clean_inspection_failed"


def test_windows_preview_classifies_reparse_guard_as_inspection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    runtime_dir = tmp_path / "git-runtime"
    runtime_dir.mkdir()
    lock_path = runtime_dir / ".install.lock"
    lock_path.write_text("", encoding="utf-8")
    manager = GitRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )
    real_lstat = Path.lstat

    def _lstat(self):
        info = real_lstat(self)
        if self != lock_path:
            return info
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_nlink=info.st_nlink,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_file_attributes=1,
        )

    monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1, raising=False)
    monkeypatch.setattr(Path, "lstat", _lstat)
    monkeypatch.setattr("core.managed_runtime.fcntl_available", lambda: False)
    monkeypatch.setattr("core.managed_runtime.try_windows_exclusive_lock", lambda _fd: True)

    result = manager.clean(dry_run=True)

    assert result["ok"] is False
    assert result["reason"] == "git_clean_inspection_failed"


def test_install_refuses_guard_replaced_after_lock_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from storage import lock as storage_lock

    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, "git")
    runtime_dir = tmp_path / "git-runtime"
    runtime_dir.mkdir()
    lock_path = runtime_dir / ".install.lock"
    lock_path.write_text("", encoding="utf-8")
    manager = GitRuntimeManager(runtime_dir=runtime_dir, manifest_path=manifest)
    real_lstat = Path.lstat
    real_try_lock = storage_lock._try_lock
    replaced = {"value": False}

    def _lstat(self):
        info = real_lstat(self)
        if replaced["value"] and self == lock_path:
            fields = list(info)
            fields[1] = info.st_ino + 73
            return os.stat_result(fields)
        return info

    def _try_lock(handle):
        acquired = real_try_lock(handle)
        if acquired:
            replaced["value"] = True
        return acquired

    monkeypatch.setattr(Path, "lstat", _lstat)
    monkeypatch.setattr(storage_lock, "_try_lock", _try_lock)

    result = manager.ensure()

    assert result["ok"] is False
    assert result["reason"] == "git_install_lock_failed"


def test_windows_preview_detects_held_git_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    runtime_dir = tmp_path / "git-runtime"
    runtime_dir.mkdir()
    (runtime_dir / ".install.lock").write_text("", encoding="utf-8")
    manager = GitRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )
    monkeypatch.setattr("core.managed_runtime.fcntl_available", lambda: False)
    monkeypatch.setattr("core.managed_runtime.try_windows_exclusive_lock", lambda fd: False)
    result = manager.clean(dry_run=True)
    assert result["ok"] is False
    assert result["reason"] == "git_install_already_running"


def test_git_preview_refuses_lock_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    runtime_dir = tmp_path / "git-runtime"
    runtime_dir.mkdir()
    lock_path = runtime_dir / ".install.lock"
    lock_path.write_text("", encoding="utf-8")
    manager = GitRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )
    real_lstat = Path.lstat
    mismatch = {"on": False}

    def _lstat(self):
        info = real_lstat(self)
        if mismatch["on"] and self == lock_path:
            fields = list(info)
            fields[1] = info.st_ino + 51
            return os.stat_result(fields)
        return info

    def _fstat(fd):
        mismatch["on"] = True
        return real_fstat(fd)

    real_fstat = os.fstat
    monkeypatch.setattr(Path, "lstat", _lstat)
    monkeypatch.setattr(os, "fstat", _fstat)
    result = manager.clean(dry_run=True)
    assert result["ok"] is False
    assert result["reason"] == "git_clean_inspection_failed"


def test_shared_ensure_failure_vocabulary_matches_reachable_reason_literals() -> None:
    module = ast.parse(Path("core/managed_runtime.py").read_text(encoding="utf-8"))
    manager = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "ManagedRuntimeManager"
    )
    methods = {
        node.name: node
        for node in manager.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # This post-publish call is exception-isolated and cannot contribute an ensure failure.
    non_failure_calls = {"_clean_after_successful_install"}
    reachable = {"ensure"}
    pending = ["ensure"]
    while pending:
        method = methods[pending.pop()]
        for node in ast.walk(method):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "self"
                and function.attr in methods
                and function.attr not in non_failure_calls
                and function.attr not in reachable
            ):
                continue
            reachable.add(function.attr)
            pending.append(function.attr)

    reason_suffixes = {
        node.args[0].value
        for method_name in reachable
        for node in ast.walk(methods[method_name])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr == "_reason"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }

    assert managed_runtime._ENSURE_FAILURE_SUFFIXES == reason_suffixes


def test_ensure_rejects_a_changed_resolved_target_before_archive_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "fixture.tar.gz"
    binary_payload = b"v1\n"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("fixture")
        member.mode = 0o755
        member.size = len(binary_payload)
        tar.addfile(member, io.BytesIO(binary_payload))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "v1",
                "source": "example/fixture",
                "archives": {
                    "linux-x64": {
                        "url": archive.as_uri(),
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "binary_sha256": hashlib.sha256(binary_payload).hexdigest(),
                        "bin_path": "fixture",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "linux-x64")
    manager = FixtureRuntimeManager(
        spec=ManagedRuntimeSpec(
            runtime_id="fixture",
            manifest_resource="unused.json",
            version_field="version",
            default_bin_path="fixture",
        ),
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest,
    )
    expected_target = {
        "runtime_version": "v1",
        "platform": "linux-x64",
        "archive_sha256": "0" * 64,
        "binary_sha256": hashlib.sha256(binary_payload).hexdigest(),
    }
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: (_ for _ in ()).throw(AssertionError("archive accessed")),
    )

    result = manager.ensure(expected_target=expected_target)

    assert result["ok"] is False
    assert result["reason"] == "fixture_install_target_changed"
