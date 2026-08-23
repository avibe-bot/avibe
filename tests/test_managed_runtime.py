from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from core import managed_runtime
from core.git_runtime import GitRuntimeManager
from core.managed_runtime import (
    ManagedRuntimeArchive,
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
)
from core.memory import artifact as memory_artifact
from core.memory.artifact import MemoryArtifactManager, MemoryRuntimeActivationError
from vibe.model_hub_runtime.installer import EngineRuntimeManager


class FixtureRuntimeManager(ManagedRuntimeManager):
    def _binary_version(self, binary: Path | None) -> str | None:
        if binary is None:
            return None
        return binary.read_text(encoding="utf-8").strip()


def _write_subclass_runtime_fixture(
    tmp_path: Path,
    runtime_kind: str,
) -> tuple[Path, Path]:
    binary_paths = {
        "git": "bin/git",
        "memory": "bin/python",
        "model-hub": "cli-proxy-api",
    }
    binary_path = binary_paths[runtime_kind]
    binary_payloads = {
        "git": b'#!/bin/sh\n[ "$1" = "--version" ] || exit 2\necho git version 2.55.0\n',
        "memory": b"#!/bin/sh\nexit 0\n",
        "model-hub": b'#!/bin/sh\n[ "$1" = "--help" ] || exit 2\necho "CLIProxyAPI Version: 7.2.95" >&2\n',
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
            "git_version": "2.55.0",
            "source": "fixture",
            "release_state": "published",
            "archives": {platform_tag: archive_payload},
        }
    elif runtime_kind == "memory":
        payload = {
            "schema_version": 1,
            "everos_version": "1.2.3",
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
            "version": "v7.2.95",
            "source": "router-for-me/CLIProxyAPI",
            "source_sha": "f71ec0eb6776854457892452cf28c47f0d658251",
            "release_tag": "v7.2.95",
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


def test_memory_retry_repairs_pointer_after_metadata_commit_outlives_pointer_failure(
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
    original_pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["source"] = "fixture-updated"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    real_write_pointer = manager._write_memory_current_pointer

    def fail_pointer_write(*_args, **_kwargs) -> None:
        raise MemoryRuntimeActivationError("pointer write failed")

    monkeypatch.setattr(manager, "_write_memory_current_pointer", fail_pointer_write)

    failed = manager.ensure(force=True)

    assert failed["ok"] is False
    metadata_after_failure = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata_after_failure["manifest_sha256"] != original_pointer["manifest_sha256"]
    assert json.loads(pointer_path.read_text(encoding="utf-8")) == original_pointer

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
    assert repaired_pointer["manifest_sha256"] == metadata_after_failure["manifest_sha256"]
    assert repaired_pointer["install_dir"] == installed["install_dir"]


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
    assert result["reason"] == "git_install_already_running"


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
