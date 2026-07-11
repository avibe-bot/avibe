from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from core import git_runtime, managed_runtime
from core.git_runtime import GitRuntimeManager
from storage.lock import MigrationFileLock


def _write_git_archive(tmp_path: Path, *, version: str = "2.55.0") -> Path:
    root = tmp_path / "archive-root" / "bin"
    root.mkdir(parents=True)
    binary = root / "git"
    binary.write_text(
        f"#!/bin/sh\n[ \"$1\" = \"--version\" ] || exit 2\necho git version {version}\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    archive = tmp_path / "git-test.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname="bin/git")
    return archive


def _write_manifest(
    tmp_path: Path,
    archive: Path,
    *,
    sha256: str | None = None,
    binary_sha256: str | None = None,
    release_state: str = "published",
    version: str = "2.55.0",
) -> Path:
    if binary_sha256 is None:
        with tarfile.open(archive, "r:gz") as tar:
            binary = tar.extractfile("bin/git")
            if binary is None:
                raise ValueError("test archive is missing bin/git")
            binary_sha256 = hashlib.sha256(binary.read()).hexdigest()
    manifest = {
        "schema_version": 1,
        "git_version": version,
        "source": "test",
        "source_url": "file://test",
        "release_state": release_state,
        "archives": {
            managed_runtime.runtime_platform_tag(): {
                "name": archive.name,
                "url": archive.as_uri(),
                "sha256": sha256 or hashlib.sha256(archive.read_bytes()).hexdigest(),
                "binary_sha256": binary_sha256,
                "size": archive.stat().st_size,
                "bin_path": "bin/git",
            }
        },
    }
    path = tmp_path / "git_runtime_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_manifest_parses_and_exposes_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)

    status = GitRuntimeManager(manifest_path=manifest).status()

    assert status["version"] == "2.55.0"
    assert status["manifest"]["git_version"] == "2.55.0"
    assert status["archive"]["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert status["archive"]["binary_sha256"] == json.loads(manifest.read_text(encoding="utf-8"))[
        "archives"
    ][managed_runtime.runtime_platform_tag()]["binary_sha256"]


def test_manifest_requires_pinned_binary_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    del payload["archives"][managed_runtime.runtime_platform_tag()]["binary_sha256"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    status = GitRuntimeManager(manifest_path=manifest).status()

    assert status["installed"] is False
    assert status["manifest"] is None
    assert status["reason"] == "git_manifest_invalid"


def test_install_verifies_archive_and_uses_versioned_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AVIBE_HOME", str(home))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = GitRuntimeManager(manifest_path=manifest)

    result = manager.ensure()

    assert result["ok"] is True
    installed = Path(result["path"])
    relative = installed.relative_to(home / "runtime" / "git")
    assert relative.parts[:3] == (
        "versions",
        "2.55.0",
        managed_runtime.runtime_platform_tag(),
    )
    assert len(relative.parts[3]) == 16
    assert relative.parts[4:] == ("bin", "git")
    assert manager.resolve_git_path() == installed


@pytest.mark.parametrize("pointer_payload", [None, "not-json"])
def test_reusing_install_repairs_current_pointer_before_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pointer_payload: str | None,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AVIBE_HOME", str(home))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = GitRuntimeManager(manifest_path=manifest)
    first = manager.ensure()
    assert first["ok"] is True
    pointer = home / "runtime" / "git" / "current.json"
    if pointer_payload is None:
        pointer.unlink()
    else:
        pointer.write_text(pointer_payload, encoding="utf-8")

    reused = manager.ensure()
    cleaned = manager.clean(keep_previous=0)

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert json.loads(pointer.read_text(encoding="utf-8"))["install_dir"] == first["install_dir"]
    assert first["install_dir"] not in cleaned["removed"]
    assert Path(first["path"]).is_file()


def test_managers_for_same_runtime_share_install_lock(tmp_path: Path) -> None:
    first = GitRuntimeManager(runtime_dir=tmp_path / "first")
    second = GitRuntimeManager(runtime_dir=tmp_path / "second")

    assert first._install_lock is second._install_lock


def test_install_and_clean_refuse_runtime_file_lock_held_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = GitRuntimeManager(manifest_path=manifest)
    external_lock = MigrationFileLock(manager._install_file_lock_path)
    external_lock.acquire()
    try:
        install_result = manager.ensure()
        clean_result = manager.clean()
    finally:
        external_lock.release()

    assert install_result["ok"] is False
    assert install_result["reason"] == "git_install_already_running"
    assert install_result["skipped"] is True
    assert clean_result == {
        "ok": False,
        "removed": [],
        "reason": "git_install_already_running",
    }


def test_checksum_mismatch_installs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive, sha256="1" * 64)
    manager = GitRuntimeManager(manifest_path=manifest)

    result = manager.ensure()

    assert result["ok"] is False
    assert result["reason"] == "git_archive_checksum_mismatch"
    assert manager.resolve_git_path() is None


def test_binary_checksum_mismatch_installs_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive, binary_sha256="1" * 64)
    manager = GitRuntimeManager(manifest_path=manifest)

    result = manager.ensure()

    assert result["ok"] is False
    assert result["reason"] == "git_binary_checksum_mismatch"
    assert manager.resolve_git_path() is None


def test_install_rejects_archive_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AVIBE_HOME", str(home))
    archive = tmp_path / "git-test.tar.gz"
    payload = b"escape"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("../escaped")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    manifest = _write_manifest(tmp_path, archive, binary_sha256="0" * 64)

    result = GitRuntimeManager(manifest_path=manifest).ensure()

    assert result["ok"] is False
    assert result["reason"] == "git_install_failed"
    assert not (home / "runtime" / "git" / "escaped").exists()


def test_resolve_rejects_tampered_installed_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = GitRuntimeManager(manifest_path=manifest)
    result = manager.ensure()
    assert result["ok"] is True
    installed = Path(result["path"])

    installed.write_text(installed.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    metadata_path = Path(result["install_dir"]) / manager.spec.metadata_filename
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["binary_sha256"] = hashlib.sha256(installed.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert manager.resolve_git_path() is None


def test_clean_preserves_current_install_and_removes_stale_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = GitRuntimeManager(manifest_path=manifest)
    first = manager.ensure()
    assert first["ok"] is True

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["build_revision"] = 2
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    second = manager.ensure()
    assert second["ok"] is True
    assert second["install_dir"] != first["install_dir"]

    cleaned = manager.clean(keep_previous=0)

    assert first["install_dir"] in cleaned["removed"]
    assert Path(second["path"]).is_file()


def test_offline_install_does_not_open_archive_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    monkeypatch.setattr(
        managed_runtime.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("offline install attempted I/O"),
    )

    result = GitRuntimeManager(manifest_path=manifest, offline=True).ensure()

    assert result["ok"] is False
    assert result["reason"] == "git_archive_unavailable_offline"


def test_offline_environment_flag_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VIBE_GIT_OFFLINE", "1")

    assert GitRuntimeManager().offline is True


def test_offline_manifest_upgrade_fails_closed_instead_of_trusting_previous_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("AVIBE_HOME", str(home))
    first_archive = _write_git_archive(tmp_path / "first", version="2.55.0")
    first_manifest = _write_manifest(tmp_path / "first", first_archive, version="2.55.0")
    first = GitRuntimeManager(manifest_path=first_manifest).ensure()
    assert first["ok"] is True

    next_archive = _write_git_archive(tmp_path / "next", version="2.56.0")
    next_manifest = _write_manifest(tmp_path / "next", next_archive, version="2.56.0")
    upgraded = GitRuntimeManager(manifest_path=next_manifest, offline=True)

    assert upgraded.resolve_git_path() is None
    result = upgraded.ensure()
    assert result["ok"] is False
    assert result["reason"] == "git_archive_unavailable_offline"
    assert Path(first["path"]).is_file()


def test_resolve_missing_runtime_never_fetches_remote_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        managed_runtime.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("resolve attempted network access"),
    )
    manager = GitRuntimeManager(manifest_url="https://example.invalid/git-manifest.json")

    assert manager.resolve_git_path() is None


def test_pending_manifest_fails_closed_before_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive, release_state="pending")
    monkeypatch.setattr(
        managed_runtime.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("pending manifest attempted archive download"),
    )

    result = GitRuntimeManager(manifest_path=manifest).ensure()

    assert result["ok"] is False
    assert result["reason"] == "git_runtime_unpublished"


def test_status_reports_platform_and_agent_resolution_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendored = tmp_path / "runtime" / "bin" / "git"
    system = tmp_path / "system" / "bin" / "git"

    class FakeManager:
        def resolve_git_path(self) -> Path:
            return vendored

        def status(self) -> dict[str, object]:
            return {"installed": True, "path": str(vendored), "version": "vendored-version"}

    monkeypatch.setattr(git_runtime, "get_git_runtime_manager", lambda: FakeManager())
    monkeypatch.setattr(git_runtime, "resolve_system_git_path", lambda: system)
    monkeypatch.setattr(git_runtime, "_probe_git_version", lambda path: pytest.fail("status executed Git"))

    status = git_runtime.git_runtime_status()

    assert status["resolution"] == "vendored"
    assert status["path"] == str(vendored)
    assert status["version"] == "vendored-version"
    assert status["agent"] == {
        "resolution": "system",
        "path": str(system),
        "version": None,
    }


def test_macos_system_git_checks_clt_before_executing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(git_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        git_runtime.shutil,
        "which",
        lambda name, path=None: "/usr/bin/git"
        if name == "git"
        else pytest.fail("unexpected PATH lookup for a support tool"),
    )

    class MissingCLT:
        returncode = 2
        stdout = ""
        stderr = "missing"

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return MissingCLT()

    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)

    assert git_runtime.resolve_system_git_path(env={"PATH": "/usr/bin"}) is None
    assert calls == [["/usr/bin/xcode-select", "-p"]]


def test_system_git_explicit_env_never_falls_back_to_parent_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/parent/process/bin")
    monkeypatch.setattr(
        git_runtime.shutil,
        "which",
        lambda *args, **kwargs: pytest.fail("explicit empty env must not search parent PATH"),
    )

    assert git_runtime.resolve_system_git_path(env={}) is None
    assert git_runtime.resolve_system_git_path(env={"PATH": ""}) is None


def test_macos_system_git_is_available_after_clt_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(git_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        git_runtime.shutil,
        "which",
        lambda name, path=None: "/usr/bin/git"
        if name == "git"
        else pytest.fail("unexpected PATH lookup for a support tool"),
    )

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class Result:
            returncode = 0
            stdout = (
                "/Library/Developer/CommandLineTools\n"
                if argv[-1] == "-p"
                else "git version 2.55.0\n"
            )
            stderr = ""

        return Result()

    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)

    assert git_runtime.resolve_system_git_path(env={"PATH": "/usr/bin"}) == Path("/usr/bin/git")
    assert calls == [["/usr/bin/xcode-select", "-p"]]


def test_macos_system_git_ignores_decoy_xcode_select(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoy = tmp_path / "xcode-select"
    marker = tmp_path / "decoy-ran"
    decoy.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    decoy.chmod(decoy.stat().st_mode | stat.S_IXUSR)
    target_path = os.pathsep.join([str(tmp_path), "/usr/bin"])
    lookups: list[tuple[str, str | None]] = []
    calls: list[list[str]] = []

    monkeypatch.setattr(git_runtime.platform, "system", lambda: "Darwin")

    def fake_which(name, path=None):
        lookups.append((name, path))
        if name == "git":
            return "/usr/bin/git"
        if name == "xcode-select":
            return str(decoy)
        return None

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == str(decoy):
            marker.touch()

        class Result:
            returncode = 0
            stdout = (
                "/Library/Developer/CommandLineTools\n"
                if argv[-1] == "-p"
                else "git version 2.55.0\n"
            )
            stderr = ""

        return Result()

    monkeypatch.setattr(git_runtime.shutil, "which", fake_which)
    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)

    assert git_runtime.resolve_system_git_path(env={"PATH": target_path}) == Path("/usr/bin/git")
    assert lookups == [("git", target_path)]
    assert calls == [["/usr/bin/xcode-select", "-p"]]
    assert not marker.exists()


def test_macos_non_system_git_is_classified_without_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(git_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        git_runtime.shutil,
        "which",
        lambda name, path=None: "/opt/homebrew/bin/git" if name == "git" else pytest.fail("unexpected CLT lookup"),
    )

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class Result:
            returncode = 0
            stdout = "git version 2.55.0\n"
            stderr = ""

        return Result()

    monkeypatch.setattr(git_runtime.subprocess, "run", fake_run)

    assert git_runtime.resolve_system_git_path(env={"PATH": "/opt/homebrew/bin"}) == Path(
        "/opt/homebrew/bin/git"
    )
    assert calls == []
