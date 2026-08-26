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

import pytest

from core.show_runtime import ShowRuntimeManager


def _make_manager(tmp_path: Path) -> ShowRuntimeManager:
    return ShowRuntimeManager(runtime_dir=tmp_path / "show-runtime", offline=True)


def _write_archive(manager: ShowRuntimeManager, sha256: str, payload: bytes, *, age_seconds: float = 3600) -> Path:
    downloads = manager.runtime_dir / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    path = downloads / f"{sha256}.tgz"
    path.write_bytes(payload)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
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
    unknown_claim = manager.runtime_dir / "downloads" / "backup.tgz.avibe-removing"
    unknown_claim.write_bytes(b"not ours")
    newline_archive = manager.runtime_dir / "downloads" / f"{_sha(6)}.tgz\n"
    newline_archive.write_bytes(b"newline")
    newline_claim = manager.runtime_dir / "downloads" / f"{_sha(7)}.tgz.avibe-removing\n"
    newline_claim.write_bytes(b"newline-claim")

    result = manager.clean()

    assert result["archives"]["removed_count"] == 1
    assert not stale.exists()
    assert packaged.exists() and temp_download.exists() and notes.exists()
    assert symlink.is_symlink() and outside.exists()
    assert directory.is_dir()
    assert unknown_claim.exists()
    assert newline_archive.exists()
    assert newline_claim.exists()


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


def test_clean_after_custom_manifest_install_prunes_stale_installs(tmp_path: Path) -> None:
    manifest_path = tmp_path / "custom-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manager = ShowRuntimeManager(
        runtime_dir=tmp_path / "show-runtime",
        offline=True,
        runtime_source="manifest-cache",
        manifest_path=manifest_path,
    )
    def _retag(install_dir: Path) -> None:
        metadata_path = next(install_dir.rglob(".vibe-show-runtime.json"))
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["manifest_source"] = str(manifest_path)
        metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    current_install = _write_install_metadata(manager, version="v2", sha256=_sha(1), mtime=0)
    rollback_install = _write_install_metadata(manager, version="v1", sha256=_sha(8), mtime=-3600)
    stale_install = _write_install_metadata(manager, version="v0", sha256=_sha(9), mtime=-999999)
    _retag(current_install)
    _retag(rollback_install)
    _retag(stale_install)
    _write_current_pointer(manager, _sha(1), install_dir=current_install)
    current_archive = _write_archive(manager, _sha(1), b"current")
    rollback_archive = _write_archive(manager, _sha(8), b"rollback")
    stale_archive = _write_archive(manager, _sha(9), b"stale-custom")

    manager._clean_after_managed_install(["node", str(current_install / "cli.js")])

    assert current_archive.exists() and current_install.exists()
    assert rollback_archive.exists() and rollback_install.exists()
    assert not stale_install.exists() and not stale_archive.exists()


def test_clean_after_custom_manifest_prunes_obsolete_sources(tmp_path: Path) -> None:
    old_manifest = tmp_path / "old-manifest.json"
    new_manifest = tmp_path / "new-manifest.json"
    old_manifest.write_text("{}", encoding="utf-8")
    new_manifest.write_text("{}", encoding="utf-8")
    manager = ShowRuntimeManager(
        runtime_dir=tmp_path / "show-runtime",
        offline=True,
        runtime_source="manifest-cache",
        manifest_path=new_manifest,
    )

    def _retag(install_dir: Path, source: str) -> None:
        metadata_path = next(install_dir.rglob(".vibe-show-runtime.json"))
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["manifest_source"] = source
        metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    current_install = _write_install_metadata(manager, version="v2", sha256=_sha(1), mtime=0)
    rollback_install = _write_install_metadata(manager, version="v1", sha256=_sha(8), mtime=-3600)
    stale_install = _write_install_metadata(manager, version="v0", sha256=_sha(9), mtime=-999999)
    _retag(current_install, str(new_manifest))
    _retag(rollback_install, str(old_manifest))
    _retag(stale_install, str(old_manifest))
    _write_current_pointer(manager, _sha(1), install_dir=current_install)
    current_archive = _write_archive(manager, _sha(1), b"current")
    rollback_archive = _write_archive(manager, _sha(8), b"rollback")
    stale_archive = _write_archive(manager, _sha(9), b"stale-old-source")

    manager._clean_after_managed_install(["node", str(current_install / "cli.js")])

    assert current_archive.exists() and current_install.exists()
    assert rollback_archive.exists() and rollback_install.exists()
    assert not stale_install.exists() and not stale_archive.exists()


def test_archive_cache_status_is_read_only(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(1), b"current")
    stale = _write_archive(manager, _sha(2), b"stale")

    report = manager.archive_cache_status()

    assert report["candidate_count"] == 1
    assert report["candidate_bytes"] == len(b"stale")
    assert stale.exists()


def test_recent_unprotected_archive_survives_mtime_guard(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    fresh_unprotected = _write_archive(manager, _sha(2), b"just downloaded", age_seconds=0)

    result = manager.clean()

    # Another process may have finalized this archive moments ago and not yet
    # written install metadata; the safety window must keep it.
    assert result["archives"]["removed_count"] == 0
    assert fresh_unprotected.exists()


def test_dry_run_preview_includes_archives_of_stale_install_dirs(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    # Current install (v2), retained rollback install (v1), stale install (v0)
    # whose only protection is its own metadata.
    current_install = _write_install_metadata(manager, version="v2", sha256=_sha(1), mtime=0)
    _write_current_pointer(manager, _sha(1), install_dir=current_install)
    _write_install_metadata(manager, version="v1", sha256=_sha(8), mtime=-3600)
    stale_install = _write_install_metadata(manager, version="v0", sha256=_sha(9), mtime=-999999)
    _write_archive(manager, _sha(1), b"current")
    _write_archive(manager, _sha(8), b"rollback")
    stale_archive = _write_archive(manager, _sha(9), b"stale-install-archive")

    dry = manager.clean(dry_run=True)
    assert dry["archives"]["candidate_count"] == 1
    assert dry["archives"]["candidate_bytes"] == len(b"stale-install-archive")
    assert stale_install.exists() and stale_archive.exists()  # dry run removed nothing

    real = manager.clean()
    # A real run removes the stale install dir and its archive together, while
    # the current and rollback archives stay protected in both modes.
    assert real["archives"]["removed_count"] == 1
    assert not stale_install.exists() and not stale_archive.exists()
    assert (manager.runtime_dir / "downloads" / f"{_sha(1)}.tgz").exists()
    assert (manager.runtime_dir / "downloads" / f"{_sha(8)}.tgz").exists()


def test_cli_clean_dry_run_is_read_only_for_git_runtime(monkeypatch, capsys) -> None:
    from vibe import cli as vibe_cli

    parser = vibe_cli.build_parser()
    args = parser.parse_args(["runtime", "clean", "--dry-run", "--json"])

    class FakeRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {"ok": True, "dry_run": dry_run, "removed": [], "archives": {"candidate_count": 0}}

    git_calls: list[dict] = []

    def fake_git_clean(*, keep_previous, dry_run=False):
        git_calls.append({"keep_previous": keep_previous, "dry_run": dry_run})
        return {"ok": True, "removed": ["git-stale"]}

    monkeypatch.setattr(vibe_cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    monkeypatch.setattr(vibe_cli, "_clean_git_runtime", fake_git_clean)

    assert vibe_cli.cmd_runtime(args) == 0
    assert git_calls == [{"keep_previous": 1, "dry_run": True}]
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["git"]["removed"] == ["git-stale"]


def test_cleanup_reuses_install_guard_without_deadlock(tmp_path: Path) -> None:
    """The post-install cleanup runs inside the installer's guard.

    ``flock`` is not re-entrant across file handles, so this exercises the
    depth-counted reuse: cleaning while the guard is held must complete and
    actually remove stale archives instead of timing out.
    """
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(1), b"current")
    stale = _write_archive(manager, _sha(2), b"stale")

    with manager._install_guard_locked():
        result = manager._clean_downloaded_archives()

    assert result["removed_count"] == 1
    assert not stale.exists()


def test_cleanup_skips_while_foreign_process_holds_install_guard(tmp_path: Path) -> None:
    from storage.lock import MigrationFileLock

    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    stale = _write_archive(manager, _sha(2), b"stale")

    foreign = MigrationFileLock(manager.runtime_dir / ".install.lock", timeout_seconds=0)
    foreign.acquire()
    try:
        result = manager._clean_downloaded_archives()
    finally:
        foreign.release()

    assert result["removed_count"] == 0
    assert result["skipped_reason"] == "runtime_install_already_running"
    assert stale.exists()


def test_archive_cache_status_is_read_only_and_creates_no_lock(tmp_path: Path) -> None:
    """Doctor/status previews must not create or rewrite runtime state."""
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(2), b"stale")

    report = manager.archive_cache_status()

    assert report["candidate_count"] == 1
    assert not (manager.runtime_dir / ".install.lock").exists()


def test_cleanup_reports_inspection_failure_distinct_from_lock_contention(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))

    def _boom(skip_metadata_under=None):
        raise OSError("disk unreadable")

    original = manager._protected_archive_sha256s
    manager._protected_archive_sha256s = _boom
    try:
        result = manager._clean_downloaded_archives()
    finally:
        manager._protected_archive_sha256s = original

    assert result["skipped_reason"] == "archive_inspection_failed"


def test_archive_cleanup_outcomes_are_fully_rendered_everywhere() -> None:
    """Enumeration pin: every report outcome/reason must render distinctly.

    A new outcome or skip reason that the CLI/Doctor forgot to wire fails this
    test instead of silently rendering placeholder counts.
    """
    from core import show_runtime as module
    from vibe import cli as vibe_cli

    reasons = {
        module._SKIPPED_ARCHIVE_REASON_INSTALL_RUNNING,
        module._SKIPPED_ARCHIVE_REASON_INSPECTION_FAILED,
        module._SKIPPED_ARCHIVE_REASON_REMOVAL_FAILED,
    }
    # Every skip reason produces a non-empty localized skip line.
    language = "en"
    rendered = set()
    for reason in reasons:
        message = vibe_cli.i18n_t("runtime.clean.skipped", language, reason=reason)
        assert message and message != "runtime.clean.skipped"
        assert reason in message
        rendered.add(message)
    assert len(rendered) == len(reasons)
    # Inspection failures carry different remediation from lock contention.
    wait_action = vibe_cli.i18n_t("runtime.doctor.archiveCacheSkippedAction", language)
    inspect_action = vibe_cli.i18n_t("runtime.doctor.archiveCacheSkippedInspectionAction", language)
    assert wait_action and inspect_action and wait_action != inspect_action
    # Partial removals report the failed count.
    partial = vibe_cli.i18n_t("runtime.clean.partiallyRemoved", language, failed=3)
    assert "3" in partial and partial != "runtime.clean.partiallyRemoved"
    # The outcome vocabulary stays closed.
    assert module._ARCHIVE_CLEANUP_OUTCOMES == frozenset({"cleaned", "partial", "skipped"})


def test_unreadable_retained_install_metadata_aborts_cleanup(tmp_path: Path) -> None:
    """A rollback install with malformed metadata must protect its archive."""
    manager = _make_manager(tmp_path)
    current_install = _write_install_metadata(manager, version="v2", sha256=_sha(1), mtime=0)
    _write_current_pointer(manager, _sha(1), install_dir=current_install)
    _write_install_metadata(manager, version="v1", sha256=_sha(8), mtime=-3600)
    (manager.runtime_dir / "versions" / "v1" / "test").mkdir(parents=True, exist_ok=True)
    # Corrupt the retained rollback install's metadata after writing it.
    rollback_dir = manager.runtime_dir / "versions" / "v1"
    for metadata in rollback_dir.rglob(".vibe-show-runtime.json"):
        metadata.write_text("{ not json", encoding="utf-8")
    _write_archive(manager, _sha(1), b"current")
    rollback_archive = _write_archive(manager, _sha(8), b"rollback")

    result = manager.clean()

    assert result["archives"]["skipped_reason"] == "archive_inspection_failed"
    assert rollback_archive.exists()


def test_archive_removal_failures_are_reported(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(2), b"stale")

    real_unlink = os.unlink

    def _unlink_boom(path, *args, **kwargs):
        raise OSError("read-only filesystem")

    real_remove = Path.unlink
    monkeypatch.setattr(Path, "unlink", _unlink_boom)
    monkeypatch.setattr("os.unlink", _unlink_boom)
    try:
        result = manager.clean()
    finally:
        monkeypatch.setattr(Path, "unlink", real_remove)
        monkeypatch.setattr("os.unlink", real_unlink)

    assert result["archives"]["skipped_reason"] == "archive_removal_failed"
    assert result["archives"]["outcome"] == "skipped"
    assert result["archives"]["candidate_count"] == 1


def test_archive_cache_status_handles_stale_install_plan_paths(tmp_path: Path) -> None:
    """Doctor status must convert the stale-plan strings back to Paths."""
    manager = _make_manager(tmp_path)
    current_install = _write_install_metadata(manager, version="v2", sha256=_sha(1), mtime=0)
    _write_current_pointer(manager, _sha(1), install_dir=current_install)
    _write_install_metadata(manager, version="v1", sha256=_sha(8), mtime=-3600)
    _write_install_metadata(manager, version="v0", sha256=_sha(9), mtime=-999999)
    _write_archive(manager, _sha(1), b"current")
    stale_archive = _write_archive(manager, _sha(9), b"stale")

    report = manager.archive_cache_status()

    assert report.get("skipped_reason") != "archive_inspection_failed"
    assert report["candidate_count"] == 1
    assert stale_archive.exists()


def test_install_guard_unavailable_falls_back_to_verified_install(tmp_path: Path, monkeypatch) -> None:
    """A read-only runtime dir must not turn a verified install into a failure."""
    from core import show_runtime as module

    monkeypatch.setattr(module, "_runtime_platform_tag", lambda: "test")
    monkeypatch.setattr(module, "_resolve_node_command", lambda: ["node"])
    manifest_payload = {
        "schema_version": 1,
        "runtime_version": "v2",
        "archives": {
            "test": {
                "name": "vibe-show-runtime-node-test.tgz",
                "url": f"file://{tmp_path}/v2.tgz",
                "sha256": _sha(1),
            }
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    manager = ShowRuntimeManager(
        runtime_dir=tmp_path / "show-runtime",
        offline=True,
        runtime_source="manifest-cache",
        manifest_path=manifest_path,
    )
    manifest = manager._load_runtime_manifest()
    assert manifest is not None
    archive = manifest.archives["test"]
    install_dir = manager._manifest_install_dir(manifest, archive)
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / ".vibe-show-runtime.json").write_text(
        json.dumps(
            {
                "provider": "manifest-cache",
                "manifest_sha256": manifest.digest,
                "runtime_version": manifest.runtime_version,
                "platform": "test",
                "archive_name": archive.name,
                "archive_sha256": archive.sha256,
            }
        ),
        encoding="utf-8",
    )
    cli_path = install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_text("runtime", encoding="utf-8")

    def _unwritable_open(path, flags, *args, **kwargs):
        if Path(path) == manager.runtime_dir / ".install.lock":
            raise OSError("read-only filesystem")
        return real_open(path, flags, *args, **kwargs)

    real_open = os.open
    monkeypatch.setattr(os, "open", _unwritable_open)

    availability, operation = manager._attempt_managed_install(
        force=False,
        offline=True,
        automatic=False,
    )
    command = availability.command

    assert command is not None and command[-1].endswith("cli.js")
    assert operation.ok is True


def test_partial_removal_reports_removed_and_failed_counts(monkeypatch, capsys) -> None:
    from vibe import cli as vibe_cli

    parser = vibe_cli.build_parser()
    args = parser.parse_args(["runtime", "clean"])

    class FakeRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {
                "ok": True,
                "removed": [],
                "archives": {
                    "outcome": "partial",
                    "candidate_count": 3,
                    "removed_count": 2,
                    "removed_bytes": 2048,
                    "failed_count": 1,
                    "skipped_reason": "archive_removal_failed",
                },
            }

    monkeypatch.setattr(vibe_cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    monkeypatch.setattr(
        vibe_cli,
        "_clean_git_runtime",
        lambda *, keep_previous, dry_run=False: {"ok": True, "removed": []},
    )

    assert vibe_cli.cmd_runtime(args) == 1
    captured = capsys.readouterr()
    assert "2" in captured.out  # successful removal total still reported
    assert "1" in captured.err  # failed count reported as a warning


def test_dry_run_with_candidates_previews_without_partial_warning(monkeypatch, capsys) -> None:
    from vibe import cli as vibe_cli

    parser = vibe_cli.build_parser()
    args = parser.parse_args(["runtime", "clean", "--dry-run"])

    class FakeRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            assert dry_run is True
            return {
                "ok": True,
                "removed": [],
                "archives": {
                    "outcome": "partial",
                    "candidate_count": 3,
                    "candidate_bytes": 3072,
                    "removed_count": 0,
                    "removed_bytes": 0,
                    "failed_count": 0,
                },
            }

    monkeypatch.setattr(vibe_cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    monkeypatch.setattr(
        vibe_cli,
        "_clean_git_runtime",
        lambda *, keep_previous, dry_run=False: {"ok": True, "removed": []},
    )

    assert vibe_cli.cmd_runtime(args) == 0
    captured = capsys.readouterr()
    assert "3" in captured.out and "3.0 KiB" in captured.out  # candidate preview rendered
    assert captured.err == ""  # no bogus partial-removal warning in a preview


def test_archive_cache_status_reports_stale_plan_failure(tmp_path: Path, monkeypatch) -> None:
    """A stale-plan phase failure returns the structured inspection report."""
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))

    def _boom(*args, **kwargs):
        raise OSError("directory disappeared")

    monkeypatch.setattr(manager, "_clean_manifest_install_dirs", _boom)
    report = manager.archive_cache_status()

    assert report.get("skipped_reason") == "archive_inspection_failed"


def test_candidate_stat_failure_is_an_inspection_failure(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(2), b"stale")

    import errno

    def _stat_boom(self):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(Path, "lstat", _stat_boom)
    result = manager.clean()

    assert result["archives"].get("skipped_reason") == "archive_inspection_failed"


def test_forced_prepare_fails_structured_when_guard_unavailable(tmp_path: Path, monkeypatch) -> None:
    from core import show_runtime as module

    monkeypatch.setattr(module, "_runtime_platform_tag", lambda: "test")
    monkeypatch.setattr(module, "_resolve_node_command", lambda: ["node"])
    manifest_payload = {
        "schema_version": 1,
        "runtime_version": "v2",
        "archives": {"test": {"name": "n.tgz", "url": "file:///n.tgz", "sha256": _sha(1)}},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    manager = ShowRuntimeManager(
        runtime_dir=tmp_path / "show-runtime",
        offline=True,
        runtime_source="manifest-cache",
        manifest_path=manifest_path,
        force_install=True,
    )

    import builtins

    real_open = builtins.open if False else os.open

    def _unwritable_open(p, flags, *args, **kwargs):
        if isinstance(p, Path) and p.name == ".install.lock":
            raise OSError("read-only filesystem")
        return real_open(p, flags, *args, **kwargs)

    monkeypatch.setattr("os.open", _unwritable_open)

    availability, operation = manager._attempt_managed_install(
        force=True,
        offline=True,
        automatic=False,
    )
    command = availability.command

    assert command is None
    assert operation.ok is False
    assert manager._install_reason == "runtime_install_guard_unavailable"


def test_installed_manifest_command_enforces_node_requirement(tmp_path: Path, monkeypatch) -> None:
    from core import show_runtime as module

    monkeypatch.setattr(module, "_runtime_platform_tag", lambda: "test")
    monkeypatch.setattr(module, "_resolve_node_command", lambda: ["node"])
    monkeypatch.setattr(module, "_node_version", lambda _command: "18.0.0")
    manifest_payload = {
        "schema_version": 1,
        "runtime_version": "v2",
        "minimum_node": "99.0.0",
        "archives": {"test": {"name": "n.tgz", "url": "file:///n.tgz", "sha256": _sha(1)}},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    manager = ShowRuntimeManager(
        runtime_dir=tmp_path / "show-runtime",
        offline=True,
        runtime_source="manifest-cache",
        manifest_path=manifest_path,
    )
    manifest = manager._load_runtime_manifest()
    assert manifest is not None
    archive = manifest.archives["test"]
    install_dir = manager._manifest_install_dir(manifest, archive)
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / ".vibe-show-runtime.json").write_text(
        json.dumps(
            {
                "provider": "manifest-cache",
                "manifest_sha256": manifest.digest,
                "runtime_version": manifest.runtime_version,
                "platform": "test",
                "archive_name": archive.name,
                "archive_sha256": archive.sha256,
            }
        ),
        encoding="utf-8",
    )
    cli_path = install_dir / "node_modules" / "@avibe" / "show-runtime" / "dist" / "cli.js"
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    cli_path.write_text("runtime", encoding="utf-8")

    command = manager._installed_manifest_runtime_command(offline=True)

    # The installed files verify, but Node is below the manifest minimum, so
    # the installed-command resolver must not hand back an unusable command.
    assert command is None


@pytest.mark.parametrize("failure_type", [OSError, ValueError])
def test_policy_skip_normalizes_installed_command_resolution_errors(
    tmp_path: Path,
    monkeypatch,
    failure_type: type[Exception],
) -> None:
    manager = ShowRuntimeManager(
        runtime_dir=tmp_path / "show-runtime",
        runtime_source="archive",
    )

    def _fail(*, offline: bool):
        raise failure_type(f"cannot resolve installed command (offline={offline})")

    monkeypatch.setattr(manager, "_installed_managed_runtime_command", _fail)

    availability = manager._publish_policy_skip("VIBE_SHOW_RUNTIME_AUTO_INSTALL")

    assert availability.command is None
    assert availability.policy_reason == "VIBE_SHOW_RUNTIME_AUTO_INSTALL"


def test_dry_run_planning_failure_returns_structured_report(tmp_path: Path, monkeypatch) -> None:
    """A mid-scan install-dir failure must not abort the CLI dry run."""
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))

    def _boom(*args, **kwargs):
        raise OSError("install dir vanished during scan")

    monkeypatch.setattr(manager, "_clean_manifest_install_dirs", _boom)
    result = manager.clean(dry_run=True)

    assert result["archives"].get("skipped_reason") == "archive_inspection_failed"


def test_skipped_archives_do_not_hide_other_cleanup_results(monkeypatch, capsys) -> None:
    from vibe import cli as vibe_cli

    parser = vibe_cli.build_parser()
    args = parser.parse_args(["runtime", "clean"])

    class FakeRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {
                "ok": True,
                "removed": ["show-install-1"],
                "archives": {
                    "outcome": "skipped",
                    "skipped_reason": "runtime_install_already_running",
                    "removed_count": 0,
                },
            }

    monkeypatch.setattr(vibe_cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    monkeypatch.setattr(
        vibe_cli,
        "_clean_git_runtime",
        lambda *, keep_previous, dry_run=False: {"ok": True, "removed": ["git-old"]},
    )

    assert vibe_cli.cmd_runtime(args) == 1
    captured = capsys.readouterr()
    assert "runtime_install_already_running" in captured.err  # archive skip reason surfaced
    assert "1" in captured.out  # Show Runtime install result still reported
    assert "git" in captured.out.lower() or "Git" in captured.out  # Git result still reported


def test_symlinked_downloads_directory_is_rejected(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    outside = tmp_path / "outside-downloads"
    outside.mkdir()
    (outside / f"{_sha(2)}.tgz").write_bytes(b"stale")
    downloads_link = manager.runtime_dir / "downloads"
    downloads_link.parent.mkdir(parents=True, exist_ok=True)
    downloads_link.symlink_to(outside)

    result = manager.clean()

    assert result["archives"].get("skipped_reason") == "archive_inspection_failed"
    assert (outside / f"{_sha(2)}.tgz").exists()  # nothing traversed via the link


def test_symlinked_install_lock_is_refused(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    (manager.runtime_dir / ".install.lock").symlink_to(victim)

    with manager._install_guard_locked() as (acquired, reason):
        assert acquired is False
        assert reason == "runtime_install_guard_unavailable"
    assert victim.read_text(encoding="utf-8") == "precious"  # link never followed


def test_downloads_dir_stat_failure_is_an_inspection_failure(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(2), b"stale")

    import errno

    real_stat = Path.stat

    def _stat_guard(self, **kwargs):
        if self.name == "downloads" and self.parent == manager.runtime_dir:
            raise OSError(errno.EACCES, "permission denied")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat_guard)
    result = manager.clean()

    assert result["archives"].get("skipped_reason") == "archive_inspection_failed"


def test_versions_dir_stat_failure_fails_closed(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_install_metadata(manager, version="v1", sha256=_sha(8), mtime=-3600)
    rollback_archive = _write_archive(manager, _sha(8), b"rollback")

    import errno

    real_stat = Path.stat

    def _stat_guard(self, **kwargs):
        if self.name == "versions" and self.parent == manager.runtime_dir:
            raise OSError(errno.EACCES, "permission denied")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat_guard)
    result = manager.clean()

    assert result["archives"].get("skipped_reason") == "archive_inspection_failed"
    assert rollback_archive.exists()  # the rollback archive stays protected
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))

    def _boom(skip_metadata_under=None):
        raise OSError("disk unreadable")

    original = manager._protected_archive_sha256s
    manager._protected_archive_sha256s = _boom
    try:
        dry = manager.clean(dry_run=True)
        status = manager.archive_cache_status()
    finally:
        manager._protected_archive_sha256s = original

    # Both read-only surfaces return a skipped report the Doctor/CLI can
    # render; neither lets the exception escape as a silent no-item result.
    assert dry["archives"]["skipped_reason"] == "archive_inspection_failed"
    assert status["skipped_reason"] == "archive_inspection_failed"


def test_cli_clean_reports_skipped_archives_without_zero_counts(monkeypatch, capsys) -> None:
    from vibe import cli as vibe_cli

    parser = vibe_cli.build_parser()
    args = parser.parse_args(["runtime", "clean"])

    class FakeRuntimeManager:
        def clean(self, *, keep_previous=1, dry_run=False):
            return {
                "ok": True,
                "removed": [],
                "archives": {"removed_count": 0, "skipped_reason": "runtime_install_already_running"},
            }

    monkeypatch.setattr(vibe_cli, "_show_runtime_manager_from_args", lambda parsed: FakeRuntimeManager())
    monkeypatch.setattr(
        vibe_cli,
        "_clean_git_runtime",
        lambda *, keep_previous, dry_run=False: {"ok": True, "removed": []},
    )

    assert vibe_cli.cmd_runtime(args) == 1
    captured = capsys.readouterr()
    assert "runtime_install_already_running" in captured.err
    # The archive skip is reported as a warning; Show/Git results still render
    # (they may legitimately be zero here — the fake removed nothing).
    assert "Removed 0 Show Runtime cache item(s)." in captured.out
    assert "downloaded Show Runtime archive" not in captured.out  # no placeholder archive line


def test_posix_downloads_descriptor_is_closed_after_every_scan(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("directory descriptors are POSIX-only")
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(1), b"current")
    stale = _write_archive(manager, _sha(2), b"stale")

    manager.clean(dry_run=True)
    assert getattr(manager, "_downloads_dir_fd", None) is None

    manager.archive_cache_status()
    assert getattr(manager, "_downloads_dir_fd", None) is None

    manager.clean()
    assert getattr(manager, "_downloads_dir_fd", None) is None
    assert not stale.exists()

    manager.clean()
    assert getattr(manager, "_downloads_dir_fd", None) is None


@pytest.mark.parametrize(
    "digest",
    [None, "", 123, "not-a-digest", "A" * 64, "g" * 64, "a" * 63],
)
def test_retained_metadata_without_valid_digest_fails_closed(tmp_path: Path, digest) -> None:
    manager = _make_manager(tmp_path)
    current_install = _write_install_metadata(manager, version="v2", sha256=_sha(1), mtime=0)
    _write_current_pointer(manager, _sha(1), install_dir=current_install)
    rollback_dir = _write_install_metadata(manager, version="v1", sha256=_sha(8), mtime=-3600)
    metadata_path = next(rollback_dir.rglob(".vibe-show-runtime.json"))
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    if digest is None:
        payload.pop("archive_sha256", None)
    else:
        payload["archive_sha256"] = digest
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    _write_archive(manager, _sha(1), b"current")
    rollback_archive = _write_archive(manager, _sha(8), b"rollback")

    result = manager.clean()

    assert result["archives"]["skipped_reason"] == "archive_inspection_failed"
    assert rollback_archive.exists()


def test_preview_guard_covers_archive_planning(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    (manager.runtime_dir / ".install.lock").write_text("", encoding="utf-8")
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(1), b"current")
    stale = _write_archive(manager, _sha(2), b"stale")
    seen: dict[str, object] = {}
    real_clean = manager._clean_downloaded_archives

    def _observe(*, dry_run=False, skip_metadata_under=None):
        seen["fd"] = getattr(manager, "_preview_guard_fd", None)
        return real_clean(dry_run=dry_run, skip_metadata_under=skip_metadata_under)

    manager._clean_downloaded_archives = _observe
    result = manager.clean(dry_run=True)

    assert result["dry_run"] is True
    assert stale.exists()
    assert result["archives"]["candidate_count"] == 1
    if os.name != "nt":
        assert seen["fd"] is not None
    assert getattr(manager, "_preview_guard_fd", None) is None


def _stat_with_ino(info: os.stat_result, ino: int) -> os.stat_result:
    fields = list(info)
    fields[1] = ino
    return os.stat_result(fields)


def test_windows_downloads_identity_mismatch_fails_closed(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(2), b"stale")
    monkeypatch.setattr("os.name", "nt")
    real_lstat = Path.lstat
    calls = {"n": 0}

    def _lstat(self):
        result = real_lstat(self)
        if self.name == "downloads" and self.parent == manager.runtime_dir:
            calls["n"] += 1
            if calls["n"] >= 3:
                return _stat_with_ino(result, result.st_ino + 1)
        return result

    monkeypatch.setattr(Path, "lstat", _lstat)
    result = manager.clean()
    assert result["archives"].get("skipped_reason") == "archive_inspection_failed"
    assert (manager.runtime_dir / "downloads" / f"{_sha(2)}.tgz").exists()


def test_install_guard_refuses_path_fd_identity_mismatch(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    real_lstat = Path.lstat
    real_fstat = os.fstat
    mismatch = {"on": False}

    def _lstat(self):
        info = real_lstat(self)
        if mismatch["on"] and self == manager.runtime_dir / ".install.lock":
            return _stat_with_ino(info, info.st_ino + 99)
        return info

    def _fstat(fd):
        mismatch["on"] = True
        return real_fstat(fd)

    monkeypatch.setattr(Path, "lstat", _lstat)
    monkeypatch.setattr(os, "fstat", _fstat)
    with manager._install_guard_locked() as (acquired, reason):
        assert acquired is False
        assert reason == "runtime_install_guard_unavailable"


def test_install_guard_refuses_swap_after_lock_acquisition(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    real_lstat = Path.lstat
    mismatch = {"on": False}

    def _lstat(self):
        info = real_lstat(self)
        if mismatch["on"] and self == manager.runtime_dir / ".install.lock":
            return _stat_with_ino(info, info.st_ino + 77)
        return info

    def _try_lock(handle):
        mismatch["on"] = True
        return True

    monkeypatch.setattr(Path, "lstat", _lstat)
    monkeypatch.setattr("core.show_runtime.storage_lock_try_lock", _try_lock)
    with manager._install_guard_locked() as (acquired, reason):
        assert acquired is False
        assert reason == "runtime_install_guard_unavailable"


def test_preview_discards_plan_when_lock_appears_mid_scan(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(2), b"stale")
    real_clean = manager._clean_downloaded_archives

    def _observe(*, dry_run=False, skip_metadata_under=None):
        (manager.runtime_dir / "manifest-live").mkdir(parents=True, exist_ok=True)
        (manager.runtime_dir / ".install.lock").write_text("busy", encoding="utf-8")
        return real_clean(dry_run=dry_run, skip_metadata_under=skip_metadata_under)

    manager._clean_downloaded_archives = _observe
    result = manager.clean(dry_run=True)
    assert result["ok"] is False
    assert result["archives"]["skipped_reason"] == "runtime_install_already_running"


def test_abandoned_windows_claim_is_reclaimed(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    leftover = manager.runtime_dir / "downloads"
    leftover.mkdir(parents=True, exist_ok=True)
    claim = leftover / f"{_sha(2)}.tgz.avibe-removing"
    claim.write_bytes(b"abandoned")
    stamp = time.time() - 3600
    os.utime(claim, (stamp, stamp))
    result = manager.clean()
    assert not claim.exists()
    assert result["archives"]["removed_count"] >= 1


def test_cleanup_removes_abandoned_claim_before_same_digest_archive(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    digest = _sha(2)
    leftover = manager.runtime_dir / "downloads"
    leftover.mkdir(parents=True, exist_ok=True)
    claim = leftover / f"{digest}.tgz.avibe-removing"
    claim.write_bytes(b"abandoned")
    archive = _write_archive(manager, digest, b"redownloaded")
    stamp = time.time() - 3600
    os.utime(claim, (stamp, stamp))
    result = manager.clean()
    assert not claim.exists()
    assert not archive.exists()
    assert result["archives"]["removed_count"] == 2
    assert result["archives"].get("skipped_reason") is None


def test_dry_run_includes_abandoned_claims(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    _write_current_pointer(manager, _sha(1))
    leftover = manager.runtime_dir / "downloads"
    leftover.mkdir(parents=True, exist_ok=True)
    claim = leftover / f"{_sha(2)}.tgz.avibe-removing"
    claim.write_bytes(b"abandoned")
    stamp = time.time() - 3600
    os.utime(claim, (stamp, stamp))
    dry = manager.clean(dry_run=True)
    assert dry["archives"]["candidate_count"] == 1
    assert dry["archives"]["candidate_bytes"] == len(b"abandoned")
    assert claim.exists()


def test_archive_cache_status_uses_preview_guard(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    (manager.runtime_dir / ".install.lock").write_text("", encoding="utf-8")
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(2), b"stale")
    seen: dict[str, object] = {}
    real_clean = manager._clean_downloaded_archives

    def _observe(*, dry_run=False, skip_metadata_under=None):
        seen["fd"] = getattr(manager, "_preview_guard_fd", None)
        return real_clean(dry_run=dry_run, skip_metadata_under=skip_metadata_under)

    manager._clean_downloaded_archives = _observe
    report = manager.archive_cache_status()
    assert report["candidate_count"] == 1
    if os.name != "nt":
        assert seen["fd"] is not None
    assert getattr(manager, "_preview_guard_fd", None) is None


def test_windows_preview_ignores_persistent_lock_file(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    (manager.runtime_dir / ".install.lock").write_text("", encoding="utf-8")
    _write_current_pointer(manager, _sha(1))
    stale = _write_archive(manager, _sha(2), b"stale")
    monkeypatch.setattr("core.show_runtime.fcntl_available", lambda: False)
    monkeypatch.setattr("core.show_runtime.try_windows_exclusive_lock", lambda fd: True)
    result = manager.clean(dry_run=True)
    assert result["ok"] is True
    assert result["archives"]["candidate_count"] == 1
    assert stale.exists()


def test_windows_preview_detects_held_lock_before_staging(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    (manager.runtime_dir / ".install.lock").write_text("", encoding="utf-8")
    _write_current_pointer(manager, _sha(1))
    stale = _write_archive(manager, _sha(2), b"stale")
    monkeypatch.setattr("core.show_runtime.fcntl_available", lambda: False)
    monkeypatch.setattr("core.show_runtime.try_windows_exclusive_lock", lambda fd: False)
    result = manager.clean(dry_run=True)
    assert result["ok"] is False
    assert result["archives"]["skipped_reason"] == "runtime_install_already_running"
    assert stale.exists()


def test_preview_probe_refuses_fifo_lock(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    manager.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(manager.runtime_dir / ".install.lock")
    _write_current_pointer(manager, _sha(1))
    _write_archive(manager, _sha(2), b"stale")
    result = manager.clean(dry_run=True)
    assert result["ok"] is False
    assert result["archives"]["skipped_reason"] == "archive_inspection_failed"


def test_clean_keeps_completed_staging_removals_on_later_failure(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    staging = manager.runtime_dir / "manifest-gone"
    staging.mkdir(parents=True)
    _write_current_pointer(manager, _sha(1))

    def _boom(*, keep_previous, dry_run=False, removed=None, **kwargs):
        raise OSError("versions unreadable")

    monkeypatch.setattr(manager, "_clean_manifest_install_dirs", _boom)
    result = manager.clean()
    assert result["ok"] is False
    assert str(staging) in result["removed"]
    assert not staging.exists()


def test_clean_keeps_completed_install_removals_when_prune_fails(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    current_install = _write_install_metadata(manager, version="v2", sha256=_sha(1), mtime=0)
    _write_current_pointer(manager, _sha(1), install_dir=current_install)
    _write_install_metadata(manager, version="v1", sha256=_sha(8), mtime=-3600)
    stale_install = _write_install_metadata(manager, version="v0", sha256=_sha(9), mtime=-999999)
    _write_archive(manager, _sha(1), b"current")

    def _boom(self, versions_dir):
        raise OSError("parent became nonempty")

    monkeypatch.setattr(ShowRuntimeManager, "_prune_empty_manifest_version_dirs", _boom)
    result = manager.clean()
    assert result["ok"] is False
    assert str(stale_install) in result["removed"]
    assert not stale_install.exists()


def test_configured_prebuilt_archive_is_protected(tmp_path: Path) -> None:
    digest = _sha(3)
    archive = tmp_path / "show-runtime" / "downloads" / f"{digest}.tgz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"prebuilt")
    stamp = time.time() - 3600
    os.utime(archive, (stamp, stamp))
    manager = ShowRuntimeManager(
        runtime_dir=tmp_path / "show-runtime",
        offline=True,
        runtime_source="archive",
        archive_path=archive,
    )
    _write_archive(manager, _sha(2), b"stale")
    result = manager.clean()
    assert archive.exists()
    assert not (manager.runtime_dir / "downloads" / f"{_sha(2)}.tgz").exists()
    assert result["archives"]["removed_count"] == 1
