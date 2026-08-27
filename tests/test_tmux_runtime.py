from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
import urllib.error
from pathlib import Path

import pytest

from core import managed_runtime, tmux_runtime
from core.managed_runtime import ManagedRuntimeManager, runtime_platform_tag
from core.tmux_runtime import TmuxRuntimeManager


RELEASED_MANIFEST_FIXTURE = (
    Path(__file__).parent / "fixtures" / "tmux_runtime" / "v3.6b-released-manifest.json"
)
RELEASED_ARTIFACT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "tmux_runtime" / "v3.6b-release-artifacts.json"
)


def _write_tmux_archive(
    tmp_path: Path,
    *,
    text: str = "#!/bin/sh\necho tmux 3.6b\n",
    bin_path: str = "tmux",
) -> Path:
    root = tmp_path / "archive-root"
    root.mkdir()
    binary = root / bin_path
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(text, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    archive = tmp_path / "tmux-test.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname=bin_path)
    return archive


def _archive_binary_sha256(archive: Path, *, bin_path: str = "tmux") -> str:
    with tarfile.open(archive, "r:gz") as tar:
        binary = tar.extractfile(bin_path)
        assert binary is not None
        return hashlib.sha256(binary.read()).hexdigest()


def _write_manifest(
    tmp_path: Path,
    archive: Path,
    *,
    sha256: str | None = None,
    size: int | None = None,
    include_binary_sha256: bool = True,
    bin_path: str = "tmux",
    version: str = "3.6b",
) -> Path:
    digest = sha256 or hashlib.sha256(archive.read_bytes()).hexdigest()
    archive_payload = {
        "name": archive.name,
        "url": archive.as_uri(),
        "sha256": digest,
        "size": archive.stat().st_size if size is None else size,
        "bin_path": bin_path,
    }
    if include_binary_sha256:
        archive_payload["binary_sha256"] = _archive_binary_sha256(archive, bin_path=bin_path)
    manifest = {
        "schema_version": 1,
        "tmux_version": version,
        "source": "test",
        "source_url": "file://test",
        "requires_utf8proc": True,
        "terminfo": "bundled-or-system",
        "archives": {runtime_platform_tag(): archive_payload},
    }
    manifest_path = tmp_path / "tmux_runtime_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_released_manifest_and_archive_fixtures_pass_through_production_reader(
    tmp_path: Path,
) -> None:
    manifest_path = Path(tmux_runtime.__file__).resolve().parents[1] / "vibe" / "tmux_runtime_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    released_manifest = json.loads(RELEASED_MANIFEST_FIXTURE.read_text(encoding="utf-8"))
    released_artifacts = json.loads(RELEASED_ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    parsed = manager._parse_manifest(
        RELEASED_MANIFEST_FIXTURE.read_bytes(),
        loaded_from="fixture:tmux-v3.6b",
    )

    assert parsed is not None
    assert hashlib.sha256(RELEASED_MANIFEST_FIXTURE.read_bytes()).hexdigest() == (
        tmux_runtime._RELEASED_PACKAGED_MANIFEST_SHA256
    )
    assert released_artifacts["release"] == {
        "tag": "v3.6b",
        "tmux_version": "3.6b",
        "manifest_name": RELEASED_MANIFEST_FIXTURE.name,
        "manifest_sha256": tmux_runtime._RELEASED_PACKAGED_MANIFEST_SHA256,
    }
    assert set(parsed.archives) == set(released_artifacts["archives"])
    for platform_tag, artifact in released_artifacts["archives"].items():
        released_archive = released_manifest["archives"][platform_tag]
        current_archive = manifest["archives"][platform_tag]
        parsed_archive = parsed.archives[platform_tag]
        assert released_archive == {
            key: artifact[key]
            for key in ("name", "url", "sha256", "size", "bin_path")
        }
        assert current_archive == artifact
        assert parsed_archive.binary_sha256 == artifact["binary_sha256"]
    assert {
        archive["sha256"]: archive["binary_sha256"]
        for archive in released_artifacts["archives"].values()
    } == tmux_runtime._RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256


def test_tmux_is_a_full_shared_manifest_consumer() -> None:
    assert issubclass(TmuxRuntimeManager, ManagedRuntimeManager)
    inherited_methods = {
        "_downloaded_archive_matches",
        "_failure",
        "_load_manifest",
        "_manifest_archive_for_platform",
        "_manifest_install_dir",
        "_resolve_manifest_archive",
        "_verified_manifest_binary",
        "_write_current_pointer",
        "_write_manifest_install_metadata",
        "ensure",
        "probe_archive_reachability",
    }
    for method_name in inherited_methods:
        assert getattr(TmuxRuntimeManager, method_name) is getattr(ManagedRuntimeManager, method_name)
    for deleted_method_name in {
        "_legacy_manifest_install_dir",
        "_manifest_install_matches",
        "_manifest_metadata_path",
    }:
        assert not hasattr(TmuxRuntimeManager, deleted_method_name)


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


def test_released_manifest_without_binary_digest_installs_through_shared_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    binary_sha256 = _archive_binary_sha256(archive)
    manifest = _write_manifest(tmp_path, archive, include_binary_sha256=False)
    monkeypatch.setattr(
        tmux_runtime,
        "_RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256",
        {archive_sha256: binary_sha256},
    )

    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    result = manager.ensure()

    assert result["ok"] is True
    metadata = json.loads(
        (Path(result["install_dir"]) / manager.spec.metadata_filename).read_text(encoding="utf-8")
    )
    assert metadata["runtime_id"] == "tmux"
    assert metadata["binary_sha256"] == binary_sha256
    assert metadata["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()


@pytest.mark.parametrize("manifest_source", ["path", "url"])
def test_custom_manifest_without_binary_digest_persists_measured_digest_for_offline_resolution(
    tmp_path: Path,
    manifest_source: str,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive, include_binary_sha256=False)
    runtime_dir = tmp_path / "runtime"
    source = (
        {"manifest_path": manifest}
        if manifest_source == "path"
        else {"manifest_url": manifest.as_uri()}
    )
    manager = TmuxRuntimeManager(runtime_dir=runtime_dir, **source)

    installed = manager.ensure()

    assert installed["ok"] is True
    installed_binary = Path(installed["path"])
    measured_digest = hashlib.sha256(installed_binary.read_bytes()).hexdigest()
    metadata = json.loads(
        (Path(installed["install_dir"]) / manager.spec.metadata_filename).read_text(
            encoding="utf-8"
        )
    )
    assert metadata["binary_sha256"] == measured_digest

    manifest.unlink()
    archive.unlink()
    for cached in (runtime_dir / "downloads").iterdir():
        cached.unlink()
    offline = TmuxRuntimeManager(runtime_dir=runtime_dir, offline=True, **source)
    assert offline.resolve_binary() == installed_binary

    installed_binary.write_bytes(installed_binary.read_bytes() + b"# tampered\n")
    installed_binary.chmod(0o755)
    assert offline.resolve_binary() is None


def test_custom_manifest_rejects_malformed_present_binary_digest(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["archives"][runtime_platform_tag()]["binary_sha256"] = "invalid"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = TmuxRuntimeManager(
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest,
    ).ensure()

    assert result["ok"] is False
    assert result["reason"] == "tmux_manifest_invalid"


def test_pre_digest_custom_fingerprint_install_is_reused_without_archive_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest_path = _write_manifest(tmp_path, archive, include_binary_sha256=False)
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    platform_tag = runtime_platform_tag()
    archive_payload = manifest_payload["archives"][platform_tag]
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    fingerprint = hashlib.sha256(
        f"{manifest_sha256}:{archive_payload['sha256']}".encode("utf-8")
    ).hexdigest()[:16]
    runtime_dir = tmp_path / "runtime"
    install_dir = runtime_dir / "versions" / "3.6b" / platform_tag / fingerprint
    install_dir.mkdir(parents=True)
    binary = install_dir / "tmux"
    binary.write_text("#!/bin/sh\necho tmux 3.6b\n", encoding="utf-8")
    binary.chmod(0o755)
    manager = TmuxRuntimeManager(runtime_dir=runtime_dir, manifest_path=manifest_path)
    legacy_metadata = {
        "provider": "manifest",
        "manifest_sha256": manifest_sha256,
        "tmux_version": "3.6b",
        "platform": platform_tag,
        "archive_name": archive_payload["name"],
        "archive_sha256": archive_payload["sha256"],
        "bin_path": archive_payload["bin_path"],
        "manifest_source": str(manifest_path),
        "source": manifest_payload["source"],
        "requires_utf8proc": True,
        "terminfo": "bundled-or-system",
    }
    metadata_path = install_dir / manager.spec.metadata_filename
    metadata_path.write_text(json.dumps(legacy_metadata), encoding="utf-8")
    metadata_before = metadata_path.read_bytes()
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("legacy install reuse accessed an archive"),
    )

    reused = manager.ensure()

    assert reused["ok"] is True
    assert reused["changed"] is False
    assert Path(reused["path"]) == binary
    assert manager.resolve_binary() == binary
    assert metadata_path.read_bytes() == metadata_before


def test_archive_download_retries_transient_network_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["archives"][runtime_platform_tag()]["url"] = "https://example.test/tmux.tar.gz"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    attempts = 0

    def opener(_request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError(ConnectionResetError("reset"))
        return io.BytesIO(archive.read_bytes())

    monkeypatch.setattr(managed_runtime.urllib.request, "urlopen", opener)
    monkeypatch.setattr("core.dependency_network.time.sleep", lambda _delay: None)

    result = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest).ensure()

    assert result["ok"] is True
    assert attempts == 2


def test_bad_checksum_is_rejected(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(
        tmp_path,
        archive,
        sha256="0" * 64,
        include_binary_sha256=False,
    )
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    result = manager.ensure()

    assert result["ok"] is False
    assert result["reason"] == "tmux_archive_checksum_mismatch"
    assert manager.resolve_binary() is None


def test_successful_archive_fetch_clears_stale_download_error_before_checksum_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive, sha256="0" * 64)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    manager._download_error = {"kind": "timeout", "message": "old timeout"}

    monkeypatch.setattr(tmux_runtime, "get_tmux_runtime_manager", lambda: manager)
    result = tmux_runtime.ensure_tmux_installed()

    assert result["reason"] == "tmux_archive_checksum_mismatch"
    assert "checksum" in result["message"]
    assert result["message"] == "tmux archive checksum did not match the pinned manifest."
    assert "old timeout" not in result["message"]
    assert result["download_error"] is None


def test_archive_probe_rejects_unsupported_scheme_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["archives"][runtime_platform_tag()]["url"] = (
        "http://user:secret@example.test/tmux.tar.gz?token=secret"
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        managed_runtime,
        "probe_url",
        lambda *_args, **_kwargs: pytest.fail("unsupported URL must not be probed"),
    )

    result = TmuxRuntimeManager(
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest,
    ).probe_archive_reachability()

    assert result == {
        "ok": False,
        "checked": False,
        "reason": "tmux_archive_url_unsupported",
        "url": "http://example.test/tmux.tar.gz",
    }


def test_install_rejects_non_runnable_binary_without_replacing_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    installed = manager.ensure()
    assert installed["ok"] is True
    install_dir = Path(installed["install_dir"])
    sentinel = install_dir / "old-install"
    sentinel.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(tmux_runtime, "_tmux_binary_version", lambda _binary: None)
    monkeypatch.setattr(tmux_runtime, "get_tmux_runtime_manager", lambda: manager)

    result = tmux_runtime.ensure_tmux_installed(force=True)
    status = manager.status()

    assert result["ok"] is False
    assert result["reason"] == "tmux_binary_not_runnable"
    assert result["message"] == "tmux runtime binary could not be executed after installation."
    assert manager.resolve_binary() is None
    assert tmux_runtime.resolve_tmux_binary() is None
    assert status["installed"] is False
    assert status["status"] == "missing"
    assert status["reason"] == "tmux_binary_not_runnable"
    assert sentinel.read_text(encoding="utf-8") == "keep me"


def test_forced_repair_replaces_canonical_install_for_pointerless_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    source_bytes = (tmp_path / "archive-root" / "tmux").read_bytes()
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    installed = manager.ensure()
    assert installed["ok"] is True
    canonical_dir = Path(installed["install_dir"])
    damaged_binary = Path(installed["path"])
    damaged_binary.write_bytes(b"damaged tmux")
    damaged_binary.chmod(0o755)

    repaired = manager.ensure(force=True)

    assert repaired["ok"] is True
    assert repaired["changed"] is True
    assert Path(repaired["install_dir"]) == canonical_dir
    repaired_binary = Path(repaired["path"])
    assert repaired_binary == canonical_dir / "tmux"
    assert repaired_binary.read_bytes() == source_bytes

    (manager.runtime_dir / "current.json").unlink()
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("pointerless recovery accessed an archive"),
    )
    assert manager.resolve_binary() == repaired_binary


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


def test_runtime_compatibility_accepts_any_runnable_tmux_version(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path, text="#!/bin/sh\necho tmux 3.5a\n")
    manifest = _write_manifest(tmp_path, archive)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    result = manager.ensure()

    assert result["ok"] is True
    assert manager.status()["version"] == "3.5a"


def test_status_reports_install_root_for_nested_binary_path(tmp_path: Path) -> None:
    bin_path = "usr/local/bin/tmux"
    archive = _write_tmux_archive(tmp_path, bin_path=bin_path)
    manifest = _write_manifest(tmp_path, archive, bin_path=bin_path)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    installed = manager.ensure()
    status = manager.status()

    assert installed["ok"] is True
    assert status["path"] == str(Path(installed["install_dir"]) / bin_path)
    assert status["install_dir"] == installed["install_dir"]


def test_status_uses_one_remote_manifest_snapshot_against_cached_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_archive = _write_tmux_archive(tmp_path, text="#!/bin/sh\necho tmux 3.6b\n")
    remote_manifest = _write_manifest(tmp_path, old_archive, version="3.6b")
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_url=remote_manifest.as_uri())
    installed = manager.ensure()
    assert installed["ok"] is True
    cached_manifest = manager._remote_manifest_cache_path()
    cached_payload = cached_manifest.read_bytes()

    new_release = tmp_path / "new-release"
    new_release.mkdir()
    new_archive = _write_tmux_archive(new_release, text="#!/bin/sh\necho tmux 3.7\n")
    new_manifest = _write_manifest(new_release, new_archive, version="3.7")
    remote_manifest.write_bytes(new_manifest.read_bytes())
    load_calls: list[tuple[bool, bool]] = []
    original_load_manifest = manager._load_manifest

    def tracked_load_manifest(*, allow_network: bool, persist_remote_cache: bool = True):
        load_calls.append((allow_network, persist_remote_cache))
        return original_load_manifest(allow_network=allow_network, persist_remote_cache=persist_remote_cache)

    monkeypatch.setattr(manager, "_load_manifest", tracked_load_manifest)

    status = manager.status()

    assert load_calls == [(True, False)]
    assert status["manifest"]["tmux_version"] == "3.7"
    assert status["installed"] is False
    assert status["status"] == "missing"
    assert cached_manifest.read_bytes() == cached_payload


def test_status_preserves_admitted_current_install_without_manifest(tmp_path: Path) -> None:
    bin_path = "usr/local/bin/tmux"
    archive = _write_tmux_archive(tmp_path, bin_path=bin_path)
    manifest = _write_manifest(tmp_path, archive, bin_path=bin_path)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    installed = manager.ensure()
    assert installed["ok"] is True
    manifest.unlink()

    resolved = manager.resolve_binary()
    status = manager.status()

    assert resolved == Path(installed["path"])
    assert status["installed"] is True
    assert status["status"] == "ready"
    assert status["path"] == str(resolved)
    assert status["install_dir"] == installed["install_dir"]
    assert status["manifest"] is None


def test_exact_released_packaged_install_is_adopted_without_archive_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released_manifest = json.loads(RELEASED_MANIFEST_FIXTURE.read_text(encoding="utf-8"))
    platform_tag = runtime_platform_tag()
    archive = released_manifest["archives"][platform_tag]
    archive_sha256 = archive["sha256"]
    binary_sha256 = tmux_runtime._RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256[archive_sha256]
    manifest_sha256 = hashlib.sha256(RELEASED_MANIFEST_FIXTURE.read_bytes()).hexdigest()
    assert manifest_sha256 == tmux_runtime._RELEASED_PACKAGED_MANIFEST_SHA256
    released_fingerprint = hashlib.sha256(
        f"{manifest_sha256}:{archive_sha256}".encode("utf-8")
    ).hexdigest()[:16]
    runtime_dir = tmp_path / "runtime"
    released_dir = runtime_dir / "versions" / "3.6b" / platform_tag / released_fingerprint
    released_dir.mkdir(parents=True)
    binary = released_dir / "tmux"
    binary.write_text("#!/bin/sh\necho tmux 3.6b\n", encoding="utf-8")
    binary.chmod(0o755)
    manager = TmuxRuntimeManager(runtime_dir=runtime_dir)
    released_metadata = {
        "provider": "manifest",
        "manifest_sha256": manifest_sha256,
        "tmux_version": "3.6b",
        "platform": platform_tag,
        "archive_name": archive["name"],
        "archive_sha256": archive_sha256,
        "bin_path": "tmux",
        "manifest_source": "package:tmux_runtime_manifest.json",
        "source": released_manifest["source"],
        "requires_utf8proc": True,
        "terminfo": "bundled-or-system",
    }
    (released_dir / manager.spec.metadata_filename).write_text(
        json.dumps(released_metadata),
        encoding="utf-8",
    )
    stale_dir = released_dir.parent / "stale"
    stale_dir.mkdir()
    (stale_dir / manager.spec.metadata_filename).write_text(
        json.dumps(released_metadata),
        encoding="utf-8",
    )
    original_file_sha256 = managed_runtime.file_sha256

    def released_file_sha256(path: Path) -> str:
        if path.resolve() == binary.resolve():
            return binary_sha256
        return original_file_sha256(path)

    monkeypatch.setattr(managed_runtime, "file_sha256", released_file_sha256)
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("released install must be adopted without archive resolution"),
    )

    result = manager.ensure()
    pointer = json.loads((manager.runtime_dir / "current.json").read_text(encoding="utf-8"))
    cleanup = manager.clean(keep_previous=0)

    assert result["ok"] is True
    assert result["changed"] is False
    assert Path(result["path"]) == binary
    assert manager.resolve_binary() == binary
    assert result["install_dir"] == str(released_dir)
    assert pointer["runtime_id"] == "tmux"
    assert pointer["install_dir"] == str(released_dir)
    assert cleanup["ok"] is True
    assert cleanup["removed"] == [str(stale_dir)]
    assert released_dir.is_dir()
    assert not stale_dir.exists()


def test_cleanup_preserves_legacy_parent_containing_current_fingerprinted_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released_manifest = json.loads(RELEASED_MANIFEST_FIXTURE.read_text(encoding="utf-8"))
    platform_tag = runtime_platform_tag()
    archive = released_manifest["archives"][platform_tag]
    archive_sha256 = archive["sha256"]
    binary_sha256 = tmux_runtime._RELEASED_BINARY_SHA256_BY_ARCHIVE_SHA256[archive_sha256]
    manifest_sha256 = hashlib.sha256(RELEASED_MANIFEST_FIXTURE.read_bytes()).hexdigest()
    runtime_dir = tmp_path / "runtime"
    legacy_parent = runtime_dir / "versions" / "3.6b" / platform_tag
    legacy_parent.mkdir(parents=True)
    legacy_binary = legacy_parent / "tmux"
    legacy_binary.write_text("#!/bin/sh\necho tmux 3.6b\n", encoding="utf-8")
    legacy_binary.chmod(0o755)
    manager = TmuxRuntimeManager(runtime_dir=runtime_dir)
    released_metadata = {
        "provider": "manifest",
        "manifest_sha256": manifest_sha256,
        "tmux_version": "3.6b",
        "platform": platform_tag,
        "archive_name": archive["name"],
        "archive_sha256": archive_sha256,
        "bin_path": "tmux",
        "manifest_source": "package:tmux_runtime_manifest.json",
        "source": released_manifest["source"],
        "requires_utf8proc": True,
        "terminfo": "bundled-or-system",
    }
    (legacy_parent / manager.spec.metadata_filename).write_text(
        json.dumps(released_metadata),
        encoding="utf-8",
    )
    replacement_archive = _write_tmux_archive(tmp_path)
    original_file_sha256 = managed_runtime.file_sha256

    def released_file_sha256(path: Path) -> str:
        if path.name == "tmux":
            return binary_sha256
        return original_file_sha256(path)

    monkeypatch.setattr(managed_runtime, "file_sha256", released_file_sha256)
    monkeypatch.setattr(manager, "_resolve_manifest_archive", lambda _archive: replacement_archive)

    repaired = manager.ensure(force=True)
    current_dir = Path(repaired["install_dir"])
    stale_sibling = legacy_parent / "stale"
    stale_sibling.mkdir()
    (stale_sibling / manager.spec.metadata_filename).write_text(
        json.dumps(released_metadata),
        encoding="utf-8",
    )
    cleanup = manager.clean(keep_previous=0)

    assert repaired["ok"] is True
    assert repaired["changed"] is True
    assert current_dir.parent == legacy_parent
    assert cleanup["ok"] is True
    assert cleanup["removed"] == [str(stale_sibling)]
    assert legacy_parent.is_dir()
    assert legacy_binary.is_file()
    assert current_dir.is_dir()
    assert not stale_sibling.exists()


def test_remote_manifest_and_archive_cache_support_offline_reuse(tmp_path: Path) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    runtime_dir = tmp_path / "runtime"
    online = TmuxRuntimeManager(runtime_dir=runtime_dir, manifest_url=manifest.as_uri())

    first = online.ensure()
    assert first["ok"] is True
    manifest.unlink()
    archive.unlink()

    offline = TmuxRuntimeManager(
        runtime_dir=runtime_dir,
        manifest_url=manifest.as_uri(),
        offline=True,
    )
    second = offline.ensure()

    assert second["ok"] is True
    assert second["changed"] is False
    assert Path(second["path"]) == Path(first["path"])


def test_macos_preparation_preserves_pinned_binary_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _write_tmux_archive(tmp_path)
    manifest = _write_manifest(tmp_path, archive)
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    source_bytes = (tmp_path / "archive-root" / "tmux").read_bytes()
    quarantine_paths: list[Path] = []

    monkeypatch.setattr(tmux_runtime, "sys_platform", lambda: "darwin")
    monkeypatch.setattr(
        tmux_runtime,
        "_strip_quarantine",
        lambda path: quarantine_paths.append(path) or {"ok": True, "changed": False},
    )

    result = manager.ensure()
    installed = Path(result["path"])
    metadata = json.loads(
        (Path(result["install_dir"]) / manager.spec.metadata_filename).read_text(encoding="utf-8")
    )

    assert result["ok"] is True
    assert result["preparation"] == {"ok": True, "changed": False}
    assert len(quarantine_paths) == 1
    assert quarantine_paths[0].name == "tmux"
    assert installed.read_bytes() == source_bytes
    assert metadata["binary_sha256"] == hashlib.sha256(source_bytes).hexdigest()

    installed.write_bytes(source_bytes + b"# changed after install\n")
    installed.chmod(0o755)
    assert manager.resolve_binary() is None


def test_tmux_cleanup_reclaims_current_and_released_staging_prefixes(tmp_path: Path) -> None:
    manager = TmuxRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    current_staging = manager.runtime_dir / "install-pending"
    released_staging = manager.runtime_dir / "manifest-pending"
    current_staging.mkdir(parents=True)
    released_staging.mkdir()

    preview = manager.clean(keep_previous=0, dry_run=True)

    assert preview["ok"] is True
    assert preview["removed"] == [str(current_staging), str(released_staging)]
    assert current_staging.is_dir()
    assert released_staging.is_dir()

    cleaned = manager.clean(keep_previous=0)

    assert cleaned["ok"] is True
    assert cleaned["removed"] == [str(current_staging), str(released_staging)]
    assert not current_staging.exists()
    assert not released_staging.exists()
