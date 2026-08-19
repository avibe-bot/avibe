from __future__ import annotations

import ast
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from core import managed_runtime
from core.git_runtime import GitRuntimeManager
from core.managed_runtime import ManagedRuntimeManager, ManagedRuntimeSpec
from core.memory.artifact import MemoryArtifactManager
from vibe.model_hub_runtime.installer import EngineRuntimeManager


class FixtureRuntimeManager(ManagedRuntimeManager):
    def _binary_version(self, binary: Path | None) -> str | None:
        if binary is None:
            return None
        return binary.read_text(encoding="utf-8").strip()


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

    def _boom(*, keep_previous, dry_run=False):
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

    def _boom(*, keep_previous, dry_run=False):
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

    def _observe(*, keep_previous, dry_run=False):
        seen["held_lock"] = getattr(manager, "_preview_held_install_lock", False)
        seen["fd"] = getattr(manager, "_preview_guard_fd", None)
        seen["lock_busy"] = not manager._install_lock.acquire(blocking=False)
        if seen["lock_busy"] is False:
            manager._install_lock.release()
        return real_clean_locked(keep_previous=keep_previous, dry_run=dry_run)

    monkeypatch.setattr(manager, "_clean_locked", _observe)
    result = manager.clean(dry_run=True)

    assert result["ok"] is True
    assert seen["held_lock"] is True
    assert seen["lock_busy"] is True
    assert getattr(manager, "_preview_held_install_lock", False) is False
    assert getattr(manager, "_preview_guard_fd", None) is None
    assert manager._install_lock.acquire(blocking=False)
    manager._install_lock.release()


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


@pytest.mark.parametrize(
    ("include_direct_platform", "expected_platform"),
    [(False, "linux-amd64"), (True, "linux-x64")],
)
def test_list_asset_manifest_prefers_direct_platform_then_falls_back_to_alias(
    tmp_path: Path,
    monkeypatch,
    include_direct_platform: bool,
    expected_platform: str,
) -> None:
    archive = tmp_path / "fixture-linux_amd64.tar.gz"
    binary_payload = b"v1\n"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("fixture")
        member.mode = 0o755
        member.size = len(binary_payload)
        tar.addfile(member, io.BytesIO(binary_payload))

    assets = [
        {
            "platform": "linux-amd64",
            "url": archive.as_uri(),
            "size_bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "binary_sha256": hashlib.sha256(binary_payload).hexdigest(),
        }
    ]
    if include_direct_platform:
        assets.append({**assets[0], "platform": "linux-x64"})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "v1",
                "source": "example/fixture",
                "assets": assets,
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
            archives_field="assets",
            archive_size_field="size_bytes",
            platform_aliases=(("linux-x64", "linux-amd64"),),
        ),
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest,
    )

    resolved_targets: list[dict[str, str]] = []
    result = manager.ensure(on_resolved=resolved_targets.append)

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["platform"] == expected_platform
    assert resolved_targets == [result["target"]]
    assert Path(result["path"]).read_bytes() == binary_payload
    assert manager.ensure()["changed"] is False
    assert manager.status()["installed"] is True


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
        "manifest_sha256": "0" * 64,
        "runtime_version": "v1",
        "platform": "linux-x64",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
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


@pytest.mark.parametrize(
    ("manager_type", "expected_reason"),
    [
        (GitRuntimeManager, "git_manifest_missing"),
        (MemoryArtifactManager, "memory-runtime_manifest_missing"),
        (EngineRuntimeManager, "model_hub_engine_manifest_missing"),
    ],
)
def test_optional_ensure_hooks_leave_each_existing_subclass_default_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_type,
    expected_reason: str,
) -> None:
    monkeypatch.delenv("AVIBE_MEMORY_DEV_RUNTIME", raising=False)
    manager = manager_type(
        runtime_dir=tmp_path / manager_type.__name__,
        manifest_path=tmp_path / "missing-manifest.json",
        offline=True,
    )

    result = manager.ensure()

    assert result["ok"] is False
    assert result["reason"] == expected_reason
