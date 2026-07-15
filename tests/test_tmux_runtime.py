from __future__ import annotations

import hashlib
import json
import stat
import io
import tarfile
import urllib.error
from pathlib import Path

import pytest

from core import tmux_runtime
from core.tmux_runtime import TmuxRuntimeManager


def _write_tmux_archive(tmp_path: Path, *, text: str = "#!/bin/sh\necho tmux 3.6b\n") -> Path:
    root = tmp_path / "archive-root"
    root.mkdir()
    binary = root / "tmux"
    binary.write_text(text, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    archive = tmp_path / "tmux-test.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname="tmux")
    return archive


def _write_manifest(tmp_path: Path, archive: Path, *, sha256: str | None = None, size: int | None = None) -> Path:
    digest = sha256 or hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "tmux_version": "3.6b",
        "source": "test",
        "source_url": "file://test",
        "requires_utf8proc": True,
        "terminfo": "bundled-or-system",
        "archives": {
            tmux_runtime._runtime_platform_tag(): {
                "name": archive.name,
                "url": archive.as_uri(),
                "sha256": digest,
                "size": archive.stat().st_size if size is None else size,
                "bin_path": "tmux",
            }
        },
    }
    manifest_path = tmp_path / "tmux_runtime_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_platform_tag_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [
        ("macosx-14.0-arm64", "ignored", "darwin-arm64"),
        ("macosx-13.0-x86_64", "ignored", "darwin-x64"),
        ("macosx-14.0-universal2", "arm64", "darwin-arm64"),
        ("linux-x86_64", "ignored", "linux-x64"),
        ("linux-aarch64", "ignored", "linux-arm64"),
    ]
    for raw_platform, machine, expected in cases:
        monkeypatch.setattr(tmux_runtime, "get_platform", lambda value=raw_platform: value)
        monkeypatch.setattr(tmux_runtime.platform, "machine", lambda value=machine: value)
        assert tmux_runtime._runtime_platform_tag() == expected


def test_download_verify_install_and_idempotent_reinstall(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    first = manager.ensure()
    assert first["ok"] is True
    assert first["changed"] is True
    installed_path = Path(first["path"])
    assert installed_path.name == "tmux"
    assert installed_path.is_file()
    assert manager.resolve_binary() == installed_path

    second = manager.ensure()
    assert second["ok"] is True
    assert second["changed"] is False
    assert Path(second["path"]) == installed_path


def test_archive_download_retries_transient_network_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["archives"][tmux_runtime._runtime_platform_tag()]["url"] = "https://example.test/tmux.tar.gz"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    attempts = 0

    def opener(_request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError(ConnectionResetError("reset"))
        return io.BytesIO(archive.read_bytes())

    monkeypatch.setattr(tmux_runtime.urllib.request, "urlopen", opener)
    monkeypatch.setattr("core.dependency_network.time.sleep", lambda _delay: None)

    result = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest).ensure()

    assert result["ok"] is True
    assert attempts == 2


def test_bad_checksum_is_rejected(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive, sha256="0" * 64)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    result = manager.ensure()

    assert result["ok"] is False
    assert result["reason"] == "tmux_archive_checksum_mismatch"
    assert manager.resolve_binary() is None


def test_successful_archive_fetch_clears_stale_download_error_before_checksum_failure(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive, sha256="0" * 64)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    manager._download_error = {"kind": "timeout", "message": "old timeout"}

    result = manager.ensure()

    assert result["reason"] == "tmux_archive_checksum_mismatch"
    assert "checksum" in result["message"]
    assert "old timeout" not in result["message"]
    assert result["download_error"] is None


def test_archive_probe_rejects_unsupported_scheme_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["archives"][tmux_runtime._runtime_platform_tag()]["url"] = (
        "http://user:secret@example.test/tmux.tar.gz?token=secret"
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        tmux_runtime,
        "probe_url",
        lambda *_args, **_kwargs: pytest.fail("unsupported URL must not be probed"),
    )

    result = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest).probe_archive_reachability()

    assert result == {
        "ok": False,
        "checked": False,
        "reason": "tmux_archive_url_unsupported",
        "url": "http://example.test/tmux.tar.gz",
    }


def test_install_rejects_non_runnable_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest_path = _write_manifest(tmp_path, archive)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest_path)
    manifest = manager._load_manifest()
    assert manifest is not None
    archive_spec = manager._manifest_archive_for_platform(manifest)
    assert archive_spec is not None
    install_dir = manager._manifest_install_dir(manifest, archive_spec)
    install_dir.mkdir(parents=True)
    sentinel = install_dir / "old-install"
    sentinel.write_text("keep me", encoding="utf-8")

    monkeypatch.setattr(tmux_runtime, "_tmux_binary_runnable", lambda _binary: False)

    result = manager.ensure(force=True)

    assert result["ok"] is False
    assert result["reason"] == "tmux_binary_not_runnable"
    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_resolve_tmux_binary_returns_none_when_absent(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    assert manager.resolve_binary() is None


def test_tmux_status_shape(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    status = manager.status()

    assert status["id"] == "tmux"
    assert status["installed"] is False
    assert status["version"] == "3.6b"
    assert status["status"] == "missing"
    assert status["manifest"]["requires_utf8proc"] is True
    assert status["archive"]["bin_path"] == "tmux"


def test_macos_codesign_path_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    calls: list[list[str]] = []

    monkeypatch.setattr(tmux_runtime, "sys_platform", lambda: "darwin")
    sign_checks = iter([False, True])
    monkeypatch.setattr(tmux_runtime, "_codesign_valid", lambda _path: next(sign_checks))
    monkeypatch.setattr(tmux_runtime, "_strip_quarantine", lambda _path: {"ok": True, "changed": False})
    monkeypatch.setattr(tmux_runtime.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "codesign" else None)

    def fake_run(argv: list[str], **_kwargs: object):
        calls.append(argv)

        class Proc:
            returncode = 0
            stdout = "tmux 3.6b\n" if argv[-1] == "-V" else ""
            stderr = ""

        return Proc()

    monkeypatch.setattr(tmux_runtime.subprocess, "run", fake_run)

    result = manager.ensure()

    assert result["ok"] is True
    assert result["signing"]["changed"] is True
    assert calls[0][:4] == ["/usr/bin/codesign", "-f", "-s", "-"]
    assert calls[0][4].endswith("/tmux")


def test_safe_extract_tar_omits_filter_before_python_312(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _write_tmux_archive(tmp_path)
    destination = tmp_path / "extract"
    destination.mkdir()
    calls: list[object] = []

    class VersionInfo(tuple):
        major = 3
        minor = 11

    with tarfile.open(archive, "r:gz") as tar:
        original_extractall = tar.extractall

        def capture_extractall(path, members=None, *, numeric_owner=False, filter=None):
            calls.append(filter)
            return original_extractall(path, members=members, numeric_owner=numeric_owner)

        monkeypatch.setattr(tmux_runtime.sys, "version_info", VersionInfo((3, 11, 0)))
        monkeypatch.setattr(tar, "extractall", capture_extractall)
        tmux_runtime._safe_extract_tar(tar, destination)

    assert calls == [None]
    assert (destination / "tmux").is_file()


def test_safe_extract_tar_uses_data_filter_on_python_312_plus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _write_tmux_archive(tmp_path)
    destination = tmp_path / "extract"
    destination.mkdir()
    calls: list[object] = []

    class VersionInfo(tuple):
        major = 3
        minor = 12

    with tarfile.open(archive, "r:gz") as tar:
        original_extractall = tar.extractall

        def capture_extractall(path, members=None, *, numeric_owner=False, filter=None):
            calls.append(filter)
            return original_extractall(path, members=members, numeric_owner=numeric_owner, filter=filter)

        monkeypatch.setattr(tmux_runtime.sys, "version_info", VersionInfo((3, 12, 0)))
        monkeypatch.setattr(tar, "extractall", capture_extractall)
        tmux_runtime._safe_extract_tar(tar, destination)

    assert calls == ["data"]
    assert (destination / "tmux").is_file()


def test_safe_extract_tar_rejects_symlink_assisted_dotdot_member(tmp_path: Path) -> None:
    archive = tmp_path / "tmux-symlink-dotdot-escape.tar.gz"
    destination = tmp_path / "extract"
    destination.mkdir()
    outside = tmp_path / "victim"

    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "."
        tar.addfile(link)

        payload = b"escaped"
        member = tarfile.TarInfo("link/../victim")
        member.size = len(payload)
        member.mode = 0o644
        tar.addfile(member, io.BytesIO(payload))

    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(ValueError, match="Unsafe tmux archive member path"):
            tmux_runtime._safe_extract_tar(tar, destination)

    assert not outside.exists()
    assert not (destination / "victim").exists()
    assert not (destination / "link").exists()


def test_safe_extract_tar_rejects_root_escaping_hard_link(tmp_path: Path) -> None:
    archive = tmp_path / "tmux-hardlink-escape.tar.gz"
    victim = tmp_path / "victim"
    victim.write_text("victim", encoding="utf-8")
    original_victim_stat = victim.stat()
    destination = tmp_path / "extract"
    destination.mkdir()

    with tarfile.open(archive, "w:gz") as tar:
        directory = tarfile.TarInfo("sub")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        hard_link = tarfile.TarInfo("sub/tmux")
        hard_link.type = tarfile.LNKTYPE
        hard_link.linkname = "../victim"
        hard_link.mode = 0o755
        tar.addfile(hard_link)

    with tarfile.open(archive, "r:gz") as tar:
        with pytest.raises(ValueError, match="Unsafe tmux archive link target"):
            tmux_runtime._safe_extract_tar(tar, destination)

    assert not (destination / "sub" / "tmux").exists()
    assert victim.stat().st_nlink == original_victim_stat.st_nlink


def test_safe_extract_tar_allows_root_relative_in_tree_hard_link(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    binary = source / "tmux-real"
    binary.write_text("#!/bin/sh\necho tmux 3.6b\n", encoding="utf-8")
    binary.chmod(0o755)
    archive = tmp_path / "tmux-hardlink-safe.tar.gz"
    destination = tmp_path / "extract"
    destination.mkdir()

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname="tmux-real")
        directory = tarfile.TarInfo("sub")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        tar.addfile(directory)
        hard_link = tarfile.TarInfo("sub/tmux")
        hard_link.type = tarfile.LNKTYPE
        hard_link.linkname = "tmux-real"
        hard_link.mode = 0o755
        tar.addfile(hard_link)

    with tarfile.open(archive, "r:gz") as tar:
        tmux_runtime._safe_extract_tar(tar, destination)

    installed = destination / "sub" / "tmux"
    assert installed.read_text(encoding="utf-8") == "#!/bin/sh\necho tmux 3.6b\n"
    assert (destination / "tmux-real").stat().st_ino == installed.stat().st_ino
