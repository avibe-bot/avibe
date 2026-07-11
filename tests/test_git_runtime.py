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
    release_state: str = "published",
) -> Path:
    manifest = {
        "schema_version": 1,
        "git_version": "2.55.0",
        "source": "test",
        "source_url": "file://test",
        "release_state": release_state,
        "archives": {
            managed_runtime.runtime_platform_tag(): {
                "name": archive.name,
                "url": archive.as_uri(),
                "sha256": sha256 or hashlib.sha256(archive.read_bytes()).hexdigest(),
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


def test_managers_for_same_runtime_share_install_lock(tmp_path: Path) -> None:
    first = GitRuntimeManager(runtime_dir=tmp_path / "first")
    second = GitRuntimeManager(runtime_dir=tmp_path / "second")

    assert first._install_lock is second._install_lock


def test_checksum_mismatch_installs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    archive = _write_git_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive, sha256="1" * 64)
    manager = GitRuntimeManager(manifest_path=manifest)

    result = manager.ensure()

    assert result["ok"] is False
    assert result["reason"] == "git_archive_checksum_mismatch"
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
    manifest = _write_manifest(tmp_path, archive)

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
            return {"installed": True, "path": str(vendored)}

    monkeypatch.setattr(git_runtime, "get_git_runtime_manager", lambda: FakeManager())
    monkeypatch.setattr(git_runtime, "resolve_system_git_path", lambda: system)
    monkeypatch.setattr(
        git_runtime,
        "_probe_git_version",
        lambda path: "vendored-version" if path == vendored else "system-version",
    )

    status = git_runtime.git_runtime_status()

    assert status["resolution"] == "vendored"
    assert status["path"] == str(vendored)
    assert status["agent"] == {
        "resolution": "system",
        "path": str(system),
        "version": "system-version",
    }


def test_macos_system_git_checks_clt_before_executing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(git_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        git_runtime.shutil,
        "which",
        lambda name, path=None: f"/usr/bin/{name}" if name in {"git", "xcode-select"} else None,
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


def test_macos_system_git_is_available_after_clt_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(git_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        git_runtime.shutil,
        "which",
        lambda name, path=None: f"/usr/bin/{name}" if name in {"git", "xcode-select"} else None,
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
    assert calls == [["/usr/bin/xcode-select", "-p"], ["/usr/bin/git", "--version"]]


def test_macos_non_system_git_does_not_require_clt(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert calls == [["/opt/homebrew/bin/git", "--version"]]


def test_agent_path_injection_only_when_system_git_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendored = tmp_path / "runtime" / "bin" / "git"
    vendored.parent.mkdir(parents=True)
    vendored.touch()

    class FakeManager:
        def resolve_git_path(self) -> Path:
            return vendored

    monkeypatch.setattr(git_runtime, "resolve_system_git_path", lambda **kwargs: None)
    env: dict[str, str] = {}

    changed = git_runtime.prepend_vendored_git_to_path(
        env,
        base_env={"PATH": os.pathsep.join(["/usr/local/bin", "/usr/bin"])},
        manager=FakeManager(),  # type: ignore[arg-type]
    )

    assert changed is True
    assert env["PATH"].split(os.pathsep)[0] == str(vendored.parent)


def test_agent_path_injection_preserves_target_environment_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendored = tmp_path / "runtime" / "bin" / "git"

    class FakeManager:
        def resolve_git_path(self) -> Path:
            return vendored

    seen_paths: list[str] = []

    def missing_system_git(*, env):
        seen_paths.append(env["PATH"])
        return None

    monkeypatch.setattr(git_runtime, "resolve_system_git_path", missing_system_git)
    env = {"PATH": "/backend/tools:/backend/bin"}

    changed = git_runtime.prepend_vendored_git_to_path(
        env,
        base_env={"PATH": "/service/bin"},
        manager=FakeManager(),  # type: ignore[arg-type]
    )

    assert changed is True
    assert seen_paths == ["/backend/tools:/backend/bin"]
    assert env["PATH"] == f"{vendored.parent}:/backend/tools:/backend/bin"


def test_agent_path_injection_never_shadows_system_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_runtime, "resolve_system_git_path", lambda **kwargs: Path("/usr/local/bin/git"))

    class UnexpectedManager:
        def resolve_git_path(self) -> Path:
            pytest.fail("vendored Git should not be resolved when system Git is present")

    env: dict[str, str] = {}
    changed = git_runtime.prepend_vendored_git_to_path(
        env,
        base_env={"PATH": "/usr/local/bin:/usr/bin"},
        manager=UnexpectedManager(),  # type: ignore[arg-type]
    )

    assert changed is False
    assert "PATH" not in env
