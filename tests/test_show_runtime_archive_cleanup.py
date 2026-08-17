"""Show Runtime content-addressed archive cache cleanup (avibe#1506 lane A).

Covers bounding ``runtime/show-runtime/downloads/`` growth: protected
current/rollback archives survive, stale content-addressed archives are
reclaimed, unknown/symlink/temp files are never touched, dry-run reports
without deleting, and the post-install hook prunes stale archives.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.show_runtime import ShowRuntimeManager


def _make_manager(tmp_path: Path) -> ShowRuntimeManager:
    return ShowRuntimeManager(runtime_dir=tmp_path / "show-runtime", offline=True)


def _write_archive(manager: ShowRuntimeManager, sha256: str, payload: bytes) -> Path:
    downloads = manager.runtime_dir / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    path = downloads / f"{sha256}.tgz"
    path.write_bytes(payload)
    return path


def _sha(index: int) -> str:
    return f"{index:064x}"


def _write_current_pointer(manager: ShowRuntimeManager, sha256: str, install_dir: Path | None = None) -> None:
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    pointer = {
        "provider": "manifest-cache",
        "runtime_version": "v1",
        "platform": "test",
        "install_dir": str(install_dir or manager.runtime_dir / "versions" / "v1" / "test" / "abc123"),
        "manifest_sha256": "m" * 64,
        "archive_sha256": sha256,
    }
    (manager.runtime_dir / "current.json").write_text(json.dumps(pointer), encoding="utf-8")


def _write_install_metadata(manager: ShowRuntimeManager, *, version: str, sha256: str, mtime: float) -> Path:
    install_dir = manager.runtime_dir / "versions" / version / "test" / version[::-1][:8]
    install_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "provider": "manifest-cache",
        "manifest_sha256": "m" * 64,
        "runtime_version": version,
        "platform": "test",
        "archive_name": "vibe-show-runtime-node-test.tgz",
        "archive_sha256": sha256,
        "manifest_source": "package:vibe/show-runtime-manifest.json",
    }
    metadata_path = install_dir / ".vibe-show-runtime.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    stamp = time.time() + mtime
    os.utime(metadata_path, (stamp, stamp))
    os.utime(install_dir, (stamp, stamp))
    return install_dir


def test_clean_removes_stale_archives_and_keeps_current_plus_rollback(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    current_sha, rollback_sha, stale_one_sha, stale_two_sha = _sha(1), _sha(2), _sha(3), _sha(4)
    rollback_install = _write_install_metadata(manager, version="v1", sha256=rollback_sha, mtime=-7200)
    current_install = _write_install_metadata(manager, version="v2", sha256=current_sha, mtime=0)
    _write_current_pointer(manager, current_sha, install_dir=current_install)

    current_archive = _write_archive(manager, current_sha, b"current")
    rollback_archive = _write_archive(manager, rollback_sha, b"rollback")
    stale_one = _write_archive(manager, stale_one_sha, b"one")
    stale_two = _write_archive(manager, stale_two_sha, b"two")

    result = manager.clean()

    archives = result["archives"]
    assert archives["removed_count"] == 2
    assert archives["removed_bytes"] == len(b"one") + len(b"two")
    assert archives["protected_count"] == 2
    assert current_archive.exists() and rollback_archive.exists()
    assert not stale_one.exists() and not stale_two.exists()
    assert current_install.exists() and rollback_install.exists()


def test_clean_dry_run_reports_candidates_without_deleting(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(1), b"current")
    stale = _write_archive(manager, _sha(2), b"stale")

    result = manager.clean(dry_run=True)

    archives = result["archives"]
    assert result["dry_run"] is True
    assert archives["candidate_count"] == 1
    assert archives["candidate_bytes"] == len(b"stale")
    assert archives["removed_count"] == 0
    assert stale.exists()


def test_archive_cleanup_never_touches_unknown_symlink_or_tmp_files(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))

    stale = _write_archive(manager, _sha(2), b"stale")
    packaged = manager.runtime_dir / "downloads" / "vibe-show-runtime-node-test.tgz"
    packaged.write_bytes(b"packaged")
    temp_download = manager.runtime_dir / "downloads" / f"{_sha(3)}.tmp"
    temp_download.write_bytes(b"partial")
    notes = manager.runtime_dir / "downloads" / "notes.txt"
    notes.write_text("keep me", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("target", encoding="utf-8")
    symlink = manager.runtime_dir / "downloads" / f"{_sha(4)}.tgz"
    symlink.symlink_to(outside)
    directory = manager.runtime_dir / "downloads" / f"{_sha(5)}.tgz"
    directory.mkdir()

    result = manager.clean()

    assert result["archives"]["removed_count"] == 1
    assert not stale.exists()
    assert packaged.exists() and temp_download.exists() and notes.exists()
    assert symlink.is_symlink() and outside.exists()
    assert directory.is_dir()


def test_clean_is_idempotent_for_archives(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(1), b"current")
    _write_archive(manager, _sha(2), b"stale")

    first = manager.clean()
    second = manager.clean()

    assert first["archives"]["removed_count"] == 1
    assert second["archives"]["removed_count"] == 0
    assert second["archives"]["candidate_count"] == 0


def test_clean_after_managed_install_prunes_stale_archives(tmp_path: Path) -> None:
    manager = ShowRuntimeManager(runtime_dir=tmp_path / "show-runtime", offline=True, runtime_source="manifest-cache")
    current_install = _write_install_metadata(manager, version="v2", sha256=_sha(1), mtime=0)
    _write_current_pointer(manager, _sha(1), install_dir=current_install)
    current_archive = _write_archive(manager, _sha(1), b"current")
    stale = _write_archive(manager, _sha(2), b"stale")

    manager._clean_after_managed_install(["node", str(current_install / "cli.js")])

    assert current_archive.exists()  # current archive intact
    assert not stale.exists()


def test_archive_cache_status_is_read_only(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(1), b"current")
    stale = _write_archive(manager, _sha(2), b"stale")

    report = manager.archive_cache_status()

    assert report["candidate_count"] == 1
    assert report["candidate_bytes"] == len(b"stale")
    assert stale.exists()
