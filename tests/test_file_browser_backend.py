from __future__ import annotations

import errno
import io
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from vibe import ui_server
from core import file_browser_service as fs
from core.file_browser_service import ConflictError, FileBrowserError
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


@pytest.fixture(autouse=True)
def files_undo_runtime(tmp_path, monkeypatch):
    from config import paths

    runtime = tmp_path / "runtime"
    monkeypatch.setattr(paths, "get_runtime_dir", lambda: runtime)
    with fs._DELETE_UNDO_LOCK:
        fs._cancel_delete_undo_expiry_timer_locked()
        fs._DELETE_UNDO_INITIALIZED = False
    yield runtime
    with fs._DELETE_UNDO_LOCK:
        fs._cancel_delete_undo_expiry_timer_locked()
        fs._DELETE_UNDO_INITIALIZED = False


def test_resolve_safe_path_expands_home_and_requires_absolute(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))

    assert fs.resolve_safe_path("~/docs").is_absolute()
    assert fs.resolve_safe_path("~/docs") == home / "docs"
    with pytest.raises(FileBrowserError) as exc:
        fs.resolve_safe_path("relative/path")
    assert exc.value.code == "invalid_path"


def test_list_directory_includes_dirs_files_hidden_and_unfollowed_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "b.txt").write_text("b", encoding="utf-8")
    (root / "a-dir").mkdir()
    (root / ".hidden").write_text("h", encoding="utf-8")
    os.symlink(root / "b.txt", root / "link")

    visible = fs.list_directory(str(root), show_hidden=False)
    assert [(item["name"], item["kind"]) for item in visible["entries"]] == [
        ("a-dir", "dir"),
        ("b.txt", "file"),
        ("link", "symlink"),
    ]
    assert visible["entries"][1]["size"] == 1
    assert visible["entries"][1]["ext"] == "txt"

    all_entries = fs.list_directory(str(root), show_hidden=True)
    assert ".hidden" in {item["name"] for item in all_entries["entries"]}

    assert fs.metadata(str(root / "link"))["kind"] == "symlink"


def test_list_directory_truncates_scan_over_hidden_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_LIST_ENTRIES", 5)
    root = tmp_path / "root"
    root.mkdir()
    for index in range(12):
        (root / f".hidden-{index}").write_text("hidden", encoding="utf-8")
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "b.txt").write_text("b", encoding="utf-8")

    result = fs.list_directory(str(root), show_hidden=False)

    assert result["truncated"] is True


def test_list_truncated_includes_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_LIST_ENTRIES", 3)
    root = tmp_path / "root"
    root.mkdir()
    for index in range(6):
        (root / f".hidden-{index}").write_text("hidden", encoding="utf-8")

    truncated = fs.list_directory(str(root), show_hidden=False)

    assert truncated["truncated"] is True
    assert truncated["entries"] == []
    assert truncated["limit"] == 3

    visible = tmp_path / "visible"
    visible.mkdir()
    (visible / "a.txt").write_text("a", encoding="utf-8")
    not_truncated = fs.list_directory(str(visible), show_hidden=False)
    assert not_truncated["truncated"] is False
    assert "limit" not in not_truncated


def test_list_directory_exact_visible_cap_is_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_LIST_ENTRIES", 3)
    root = tmp_path / "root"
    root.mkdir()
    for index in range(3):
        (root / f"{index}.txt").write_text(str(index), encoding="utf-8")

    result = fs.list_directory(str(root), show_hidden=False)

    assert [item["name"] for item in result["entries"]] == ["0.txt", "1.txt", "2.txt"]
    assert result["truncated"] is False
    assert "limit" not in result


def test_list_directory_over_visible_cap_is_truncated_after_full_page(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_LIST_ENTRIES", 3)
    root = tmp_path / "root"
    root.mkdir()
    for index in range(4):
        (root / f"{index}.txt").write_text(str(index), encoding="utf-8")

    result = fs.list_directory(str(root), show_hidden=False)

    assert len(result["entries"]) == 3
    assert result["truncated"] is True
    assert result["limit"] == 3


def test_entry_ops_handle_cyclic_symlink(tmp_path):
    link = tmp_path / "loop"
    link.symlink_to(link)

    assert fs.metadata(str(link))["kind"] == "symlink"

    fs.delete_path(str(link))

    assert not link.is_symlink()


def test_list_rejects_traversal_to_non_directory(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    file_path = root / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(FileBrowserError) as exc:
        fs.list_directory(str(root / ".." / "root" / "file.txt"))
    assert exc.value.code == "not_dir"


def test_write_refuses_to_follow_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    real = root / "real.txt"
    real.write_text("original", encoding="utf-8")
    link = root / "link.txt"
    os.symlink(real, link)

    with pytest.raises(FileBrowserError) as exc:
        fs.write_file(str(link), "hacked")
    assert exc.value.code == "is_symlink"
    # The symlink's target must be left untouched (no write-through).
    assert real.read_text(encoding="utf-8") == "original"


def test_content_inline_headers_attachment_and_size_cap(tmp_path):
    text_path = tmp_path / "note.txt"
    text_path.write_text("hello", encoding="utf-8")
    html_path = tmp_path / "page.html"
    html_path.write_text("<script></script>", encoding="utf-8")
    large_path = tmp_path / "large.txt"
    large_path.write_bytes(b"x" * (fs.MAX_FILE_BYTES + 1))

    text = fs.file_content(str(text_path))
    assert text.mime == "text/plain"
    assert text.disposition == "inline"
    assert text.data == b"hello"

    html = fs.file_content(str(html_path))
    assert html.disposition == "attachment"

    with pytest.raises(FileBrowserError) as exc:
        fs.file_content(str(large_path))
    assert exc.value.code == "too_large"


def test_content_refuses_toctou_symlink_swap(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("other", encoding="utf-8")

    def resolve_then_swap(raw: str) -> Path:
        resolved = fs.resolve_safe_path(raw)
        target.unlink()
        target.symlink_to(other)
        return resolved

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(fs, "_resolve_existing_path", resolve_then_swap)
        with pytest.raises(FileBrowserError) as exc:
            fs.file_content(str(target))
    assert exc.value.code == "not_found"


def test_rename_no_replace_moves_when_target_absent(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("A", encoding="utf-8")
    dst = tmp_path / "b.txt"

    fs._rename_no_replace(src, dst)

    assert dst.read_text(encoding="utf-8") == "A"
    assert not src.exists()


def test_rename_refuses_to_clobber_target_appearing_after_precheck(tmp_path, monkeypatch):
    # TOCTOU guard: even if the existence pre-check is blind to the destination (it was
    # created in the race window), the atomic no-replace rename must refuse rather than
    # silently clobber the file that appeared.
    src = tmp_path / "src.txt"
    src.write_text("SRC", encoding="utf-8")
    dst = tmp_path / "dst.txt"
    dst.write_text("DST", encoding="utf-8")

    real_exists = fs._exists_no_follow
    monkeypatch.setattr(fs, "_exists_no_follow", lambda p: False if Path(p) == dst else real_exists(p))

    with pytest.raises(FileBrowserError) as exc:
        fs.rename_path(str(src), "dst.txt")

    assert exc.value.code == "exists"
    assert dst.read_text(encoding="utf-8") == "DST"  # not clobbered
    assert src.read_text(encoding="utf-8") == "SRC"  # source intact


def test_delete_refuses_filesystem_root(monkeypatch):
    # A recursive delete of "/" (or a drive root) must be refused before it can rmtree the
    # machine. Resolver + rmtree are stubbed so the test is safe even if the guard regresses.
    monkeypatch.setattr(fs, "_resolve_existing_entry_path", lambda raw: Path("/"))
    rmtree_calls: list = []
    monkeypatch.setattr(fs.shutil, "rmtree", lambda *args, **kwargs: rmtree_calls.append(args))

    with pytest.raises(FileBrowserError) as exc:
        fs.delete_path("/", recursive=True)

    assert exc.value.code == "invalid_path"
    assert rmtree_calls == []  # the guard fired before any rmtree


def test_mutation_entry_paths_reject_dotdot_final_component(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    child = parent / "child"
    child.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    rmtree_calls: list[tuple] = []

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(fs.shutil, "rmtree", lambda *args, **kwargs: rmtree_calls.append(args))
        for raw_path in ("/tmp/..", str(child / "..")):
            with pytest.raises(FileBrowserError) as exc:
                fs.delete_path(raw_path, recursive=True)
            assert exc.value.code == "invalid_path"

        with pytest.raises(FileBrowserError) as rename_exc:
            fs.rename_path(str(child / ".."), "renamed")
        assert rename_exc.value.code == "invalid_path"

        with pytest.raises(FileBrowserError) as move_source_exc:
            fs.move_path(str(child / ".."), str(tmp_path / "moved-source"))
        assert move_source_exc.value.code == "invalid_path"

        with pytest.raises(FileBrowserError) as move_target_exc:
            fs.move_path(str(source), str(child / ".."))
        assert move_target_exc.value.code == "invalid_path"

    assert rmtree_calls == []


def test_nested_delete_and_rename_still_work_for_normal_entries(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    child = nested / "child.txt"
    child.write_text("child", encoding="utf-8")

    renamed = fs.rename_path(str(child), "renamed.txt")
    renamed_path = Path(renamed["path"])

    assert renamed_path.read_text(encoding="utf-8") == "child"
    fs.delete_path(str(nested), recursive=True)
    assert not nested.exists()


def test_move_symlink_over_directory_is_refused(tmp_path):
    # overwrite=True must not let a non-directory replace a directory. is_file() follows
    # symlinks, so a symlink-to-dir (or broken link) slipped past the old guard and the move
    # then backed up + deleted the real directory's contents. No-follow guard refuses it.
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(other_dir)  # a symlink whose target is a directory
    target_dir = tmp_path / "data"
    target_dir.mkdir()
    (target_dir / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(link), str(target_dir), overwrite=True)

    assert exc.value.code == "exists"
    assert target_dir.is_dir()
    assert (target_dir / "keep.txt").read_text(encoding="utf-8") == "keep"  # contents not erased
    assert link.is_symlink()


def test_rename_same_name_is_noop(tmp_path):
    src = tmp_path / "same.txt"
    src.write_text("SRC", encoding="utf-8")

    result = fs.rename_path(str(src), "same.txt")

    assert result == {"ok": True, "path": str(src)}
    assert src.read_text(encoding="utf-8") == "SRC"

    dst = tmp_path / "other.txt"
    dst.write_text("DST", encoding="utf-8")
    with pytest.raises(FileBrowserError) as exc:
        fs.rename_path(str(src), "other.txt")

    assert exc.value.code == "exists"
    assert src.read_text(encoding="utf-8") == "SRC"
    assert dst.read_text(encoding="utf-8") == "DST"


def test_rename_to_different_hard_link_of_same_inode_is_conflict(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    target = tmp_path / "target.txt"
    os.link(source, target)

    with pytest.raises(FileBrowserError) as exc:
        fs.rename_path(str(source), "target.txt")

    assert exc.value.code == "exists"
    assert source.read_text(encoding="utf-8") == "source"
    assert target.read_text(encoding="utf-8") == "source"
    assert fs._same_entry_no_follow(source, target)


@pytest.mark.skipif(sys.platform != "darwin", reason="case-only rename behavior depends on case-insensitive filesystem")
def test_rename_case_only_same_inode_is_allowed_on_case_insensitive_fs(tmp_path):
    source = tmp_path / "case.txt"
    source.write_text("case", encoding="utf-8")
    target = tmp_path / "CASE.txt"
    if not target.exists() or not fs._same_entry_no_follow(source, target):
        pytest.skip("temporary filesystem is case-sensitive")

    result = fs.rename_path(str(source), "CASE.txt")

    assert result == {"ok": True, "path": str(target)}
    assert target.read_text(encoding="utf-8") == "case"


def test_rename_no_replace_refuses_existing_directory_target(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()

    def target_exists(*_args, **_kwargs):
        raise FileExistsError(str(dst))

    real_exists = fs._exists_no_follow
    monkeypatch.setattr(fs, "_glibc_renameat2_noreplace", target_exists)
    monkeypatch.setattr(fs, "_exists_no_follow", lambda p: False if Path(p) == dst else real_exists(p))

    with pytest.raises(FileBrowserError) as exc:
        fs._rename_no_replace(src, dst)

    assert exc.value.code == "exists"
    assert src.is_dir()
    assert dst.is_dir()


def test_write_is_atomic_and_detects_mtime_conflict(tmp_path):
    path = tmp_path / "doc.txt"
    first = fs.write_file(str(path), "first")
    assert path.read_text(encoding="utf-8") == "first"

    fs.write_file(str(path), "second", expected_mtime=first["mtime"])
    assert path.read_text(encoding="utf-8") == "second"

    with pytest.raises(FileBrowserError) as exc:
        fs.write_file(str(path), "stale", expected_mtime=first["mtime"])
    assert exc.value.code == "conflict"

    with pytest.raises(FileBrowserError) as large_exc:
        fs.write_file(str(tmp_path / "large.txt"), "x" * (fs.MAX_FILE_BYTES + 1))
    assert large_exc.value.code == "too_large"
    assert not list(tmp_path.glob(".large.txt.*.tmp"))


def test_write_maps_mkstemp_permission_error(tmp_path, monkeypatch):
    path = tmp_path / "doc.txt"

    def deny_tempfile(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(fs.tempfile, "mkstemp", deny_tempfile)

    with pytest.raises(FileBrowserError) as exc:
        fs.write_file(str(path), "blocked")

    assert exc.value.code == "permission_denied"
    assert exc.value.status_code == 403
    assert not path.exists()


def test_write_maps_mkstemp_os_error(tmp_path, monkeypatch):
    path = tmp_path / "doc.txt"

    def fail_tempfile(*_args, **_kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr(fs.tempfile, "mkstemp", fail_tempfile)

    with pytest.raises(FileBrowserError) as exc:
        fs.write_file(str(path), "blocked")

    assert exc.value.code == "fs_error"
    assert not path.exists()


def test_write_normal_path_still_succeeds(tmp_path):
    path = tmp_path / "doc.txt"

    result = fs.write_file(str(path), "ok")

    assert result["ok"] is True
    assert path.read_text(encoding="utf-8") == "ok"


def test_write_create_only_refuses_to_clobber_existing(tmp_path):
    path = tmp_path / "new.txt"

    # A brand-new file with create_only succeeds.
    result = fs.write_file(str(path), "first", create_only=True)
    assert result["ok"] is True
    assert path.read_text(encoding="utf-8") == "first"

    # A second create_only write for the same name is refused and must NOT truncate the file.
    with pytest.raises(fs.ConflictError) as excinfo:
        fs.write_file(str(path), "second", create_only=True)
    assert excinfo.value.code == "exists"
    assert path.read_text(encoding="utf-8") == "first"


def test_write_long_legal_basename_uses_bounded_temp_name(tmp_path, monkeypatch):
    name = "a" * 250
    path = tmp_path / name
    try:
        path.touch()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        name = "a" * 120
        path = tmp_path / name
        path.touch()
    path.write_text("old", encoding="utf-8")
    temp_prefixes: list[str] = []
    real_mkstemp = fs.tempfile.mkstemp

    def capture_mkstemp(*args, **kwargs):
        temp_prefixes.append(kwargs["prefix"])
        assert len(kwargs["prefix"].encode()) + 8 + len(kwargs["suffix"].encode()) <= 255
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(fs.tempfile, "mkstemp", capture_mkstemp)

    result = fs.write_file(str(path), "new")

    assert result["ok"] is True
    assert path.read_text(encoding="utf-8") == "new"
    assert temp_prefixes == [fs._WRITE_TEMP_PREFIX]


def test_write_preserves_existing_file_mode(tmp_path):
    path = tmp_path / "script.sh"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)

    fs.write_file(str(path), "#!/bin/sh\necho ok\n")

    assert path.read_text(encoding="utf-8") == "#!/bin/sh\necho ok\n"
    assert path.stat().st_mode & 0o777 == 0o755


def test_write_serializes_expected_mtime_check_and_replace(tmp_path, monkeypatch):
    path = tmp_path / "doc.txt"
    path.write_text("base", encoding="utf-8")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    expected_mtime = fs._mtime_seconds(path.stat())
    real_replace = fs.os.replace
    first_replace_entered = threading.Event()
    release_first_replace = threading.Event()
    replace_calls: list[str] = []
    replace_calls_lock = threading.Lock()
    results: list[dict[str, object]] = []
    errors: list[FileBrowserError] = []

    def blocking_replace(src: str, dst: Path) -> None:
        with replace_calls_lock:
            call_index = len(replace_calls)
            replace_calls.append(Path(src).name)
        if call_index == 0:
            first_replace_entered.set()
            assert release_first_replace.wait(2)
        real_replace(src, dst)

    def write_content(content: str) -> None:
        try:
            results.append(fs.write_file(str(path), content, expected_mtime=expected_mtime))
        except FileBrowserError as exc:
            errors.append(exc)

    monkeypatch.setattr(fs.os, "replace", blocking_replace)
    first = threading.Thread(target=write_content, args=("first",))
    first.start()
    assert first_replace_entered.wait(2)

    second = threading.Thread(target=write_content, args=("second",))
    second.start()
    time.sleep(0.1)
    assert len(replace_calls) == 1

    release_first_replace.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert [error.code for error in errors] == ["conflict"]
    assert len(results) == 1
    assert len(replace_calls) == 1
    assert path.read_text(encoding="utf-8") == "first"


def test_upload_file_happy_path_and_name_override(tmp_path):
    result = fs.upload_file(str(tmp_path), io.BytesIO(b"hello"), filename="ignored.txt", name="saved.bin")

    uploaded = tmp_path / "saved.bin"
    assert result["name"] == "saved.bin"
    assert result["path"] == str(uploaded)
    assert result["size"] == 5
    assert result["mtime"] == fs._mtime_seconds(uploaded.stat())
    assert uploaded.read_bytes() == b"hello"


def test_upload_file_conflict_without_overwrite_and_replaces_with_overwrite(tmp_path):
    target = tmp_path / "report.bin"
    target.write_bytes(b"old")
    target.chmod(0o744)

    with pytest.raises(FileBrowserError) as exists:
        fs.upload_file(str(tmp_path), io.BytesIO(b"new"), filename="report.bin")
    assert exists.value.code == "exists"
    assert target.read_bytes() == b"old"

    result = fs.upload_file(str(tmp_path), io.BytesIO(b"new"), filename="report.bin", overwrite=True)

    assert result["name"] == "report.bin"
    assert result["size"] == 3
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o777 == 0o744


def test_upload_new_file_does_not_bypass_restrictive_umask(tmp_path):
    old_umask = os.umask(0o022)
    try:
        shared = fs.upload_file(str(tmp_path), io.BytesIO(b"shared"), filename="shared.bin")
    finally:
        os.umask(old_umask)

    shared_upload = Path(shared["path"])
    assert shared_upload.read_bytes() == b"shared"
    assert shared_upload.stat().st_mode & 0o777 == 0o644

    old_umask = os.umask(0o077)
    try:
        private = fs.upload_file(str(tmp_path), io.BytesIO(b"private"), filename="private.bin")
    finally:
        os.umask(old_umask)

    private_upload = Path(private["path"])
    assert private_upload.read_bytes() == b"private"
    assert private_upload.stat().st_mode & 0o777 == 0o600


def test_upload_uses_stable_directory_after_validation(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    attacker_dir = tmp_path / "attacker"
    attacker_dir.mkdir()
    moved_upload_dir = tmp_path / "uploads-original"
    real_open_directory = fs._open_stable_upload_directory

    def open_then_swap(directory: Path) -> int:
        fd = real_open_directory(directory)
        upload_dir.rename(moved_upload_dir)
        upload_dir.symlink_to(attacker_dir, target_is_directory=True)
        return fd

    monkeypatch.setattr(fs, "_open_stable_upload_directory", open_then_swap)

    result = fs.upload_file(str(upload_dir), io.BytesIO(b"safe"), filename="safe.bin")

    assert result["path"] == str(upload_dir / "safe.bin")
    assert (moved_upload_dir / "safe.bin").read_bytes() == b"safe"
    assert not (attacker_dir / "safe.bin").exists()


def test_upload_create_only_falls_back_when_hard_links_unsupported(tmp_path, monkeypatch):
    # FAT/exFAT/SMB-style filesystems refuse os.link; create-only publish must degrade to
    # the guarded replace instead of failing the upload outright.
    def refuse_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(fs.os, "link", refuse_link)

    result = fs.upload_file(str(tmp_path), io.BytesIO(b"payload"), filename="fallback.bin")
    uploaded = tmp_path / "fallback.bin"
    assert result["path"] == str(uploaded)
    assert uploaded.read_bytes() == b"payload"
    assert not list(tmp_path.glob(f"{fs._WRITE_TEMP_PREFIX}*.tmp"))

    # The fallback still refuses to replace an existing target (create-only contract).
    with pytest.raises(ConflictError) as exc:
        fs.upload_file(str(tmp_path), io.BytesIO(b"other"), filename="fallback.bin")
    assert exc.value.code == "exists"
    assert uploaded.read_bytes() == b"payload"
    assert not list(tmp_path.glob(f"{fs._WRITE_TEMP_PREFIX}*.tmp"))


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="requires POSIX non-root permissions")
def test_upload_permission_denied_maps_to_403(tmp_path):
    read_only = tmp_path / "readonly"
    read_only.mkdir()
    read_only.chmod(0o555)
    try:
        with pytest.raises(FileBrowserError) as exc:
            fs.upload_file(str(read_only), io.BytesIO(b"x"), filename="denied.bin")
        assert exc.value.code == "permission_denied"
        assert exc.value.status_code == 403
        assert list(read_only.iterdir()) == []
    finally:
        read_only.chmod(0o755)


def test_upload_works_without_directory_fd(tmp_path, monkeypatch):
    # Platforms that cannot open a directory fd (native Windows) degrade to path-based
    # operation; the upload and its create-only contract must still work there.
    monkeypatch.setattr(fs, "_open_stable_upload_directory", lambda directory: None)

    result = fs.upload_file(str(tmp_path), io.BytesIO(b"portable"), filename="nofd.bin")
    uploaded = tmp_path / "nofd.bin"
    assert result["path"] == str(uploaded)
    assert uploaded.read_bytes() == b"portable"
    assert result["mtime"] == fs._mtime_seconds(uploaded.stat())

    with pytest.raises(ConflictError) as exc:
        fs.upload_file(str(tmp_path), io.BytesIO(b"clash"), filename="nofd.bin")
    assert exc.value.code == "exists"

    replaced = fs.upload_file(str(tmp_path), io.BytesIO(b"replaced"), filename="nofd.bin", overwrite=True)
    assert replaced["size"] == len(b"replaced")
    assert uploaded.read_bytes() == b"replaced"
    assert not list(tmp_path.glob(f"{fs._WRITE_TEMP_PREFIX}*.tmp"))


def test_upload_file_size_cap_cleans_temp_and_target(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_FILE_BYTES", 4)

    with pytest.raises(FileBrowserError) as exc:
        fs.upload_file(str(tmp_path), io.BytesIO(b"12345"), filename="large.bin")

    assert exc.value.code == "too_large"
    assert exc.value.status_code == 413
    assert not (tmp_path / "large.bin").exists()
    assert not list(tmp_path.glob(f"{fs._WRITE_TEMP_PREFIX}*.tmp"))


@pytest.mark.parametrize(
    "bad_name",
    ["../escape.txt", "nested/file.txt", "nested\\file.txt", ".", "..", "", "nul\x00byte.txt"],
)
def test_upload_file_rejects_invalid_names(tmp_path, bad_name):
    with pytest.raises(FileBrowserError) as exc:
        fs.upload_file(str(tmp_path), io.BytesIO(b"x"), filename=bad_name)

    assert exc.value.code == "invalid_name"
    assert list(tmp_path.iterdir()) == []


def test_upload_file_rejects_bad_destination_dir(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(FileBrowserError) as missing_exc:
        fs.upload_file(str(missing), io.BytesIO(b"x"), filename="upload.bin")
    assert missing_exc.value.code == "not_found"

    destination_file = tmp_path / "not-a-dir"
    destination_file.write_text("x", encoding="utf-8")
    with pytest.raises(FileBrowserError) as file_exc:
        fs.upload_file(str(destination_file), io.BytesIO(b"x"), filename="upload.bin")
    assert file_exc.value.code == "not_dir"


def test_upload_file_refuses_symlinked_dir_and_target_symlink(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link-dir"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(FileBrowserError) as dir_exc:
        fs.upload_file(str(link_dir), io.BytesIO(b"x"), filename="upload.bin")
    assert dir_exc.value.code == "not_dir"
    assert not (real_dir / "upload.bin").exists()

    target = tmp_path / "target.bin"
    target.write_bytes(b"target")
    link = tmp_path / "link.bin"
    link.symlink_to(target)

    with pytest.raises(FileBrowserError) as target_exc:
        fs.upload_file(str(tmp_path), io.BytesIO(b"new"), filename="link.bin", overwrite=True)
    assert target_exc.value.code == "exists"
    assert link.is_symlink()
    assert target.read_bytes() == b"target"


def test_upload_file_read_failure_leaves_no_partial_file(tmp_path):
    class FailingStream:
        def __init__(self) -> None:
            self.calls = 0

        def read(self, _size: int) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"partial"
            raise OSError("read failed")

    with pytest.raises(FileBrowserError) as exc:
        fs.upload_file(str(tmp_path), FailingStream(), filename="partial.bin")

    assert exc.value.code == "fs_error"
    assert not (tmp_path / "partial.bin").exists()
    assert not list(tmp_path.glob(f"{fs._WRITE_TEMP_PREFIX}*.tmp"))


def test_copy_file_and_directory_preserve_content_and_modes(tmp_path):
    source_file = tmp_path / "source.txt"
    source_file.write_text("source", encoding="utf-8")
    source_file.chmod(0o640)
    copied_file = tmp_path / "copied.txt"

    assert fs.copy_path(str(source_file), str(copied_file)) == {"ok": True, "path": str(copied_file)}
    assert source_file.read_text(encoding="utf-8") == "source"
    assert copied_file.read_text(encoding="utf-8") == "source"
    assert copied_file.stat().st_mode & 0o777 == 0o640

    source_dir = tmp_path / "source-dir"
    nested = source_dir / "nested"
    nested.mkdir(parents=True)
    nested.chmod(0o750)
    child = nested / "child.bin"
    child.write_bytes(b"child")
    child.chmod(0o600)
    copied_dir = tmp_path / "copied-dir"

    assert fs.copy_path(str(source_dir), str(copied_dir)) == {"ok": True, "path": str(copied_dir)}
    assert (copied_dir / "nested" / "child.bin").read_bytes() == b"child"
    assert (copied_dir / "nested").stat().st_mode & 0o777 == 0o750
    assert (copied_dir / "nested" / "child.bin").stat().st_mode & 0o777 == 0o600


def test_copy_conflict_without_overwrite_and_replaces_with_overwrite(tmp_path):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")

    with pytest.raises(ConflictError) as exc:
        fs.copy_path(str(source), str(destination))

    assert exc.value.code == "exists"
    assert destination.read_text(encoding="utf-8") == "old"
    assert fs.copy_path(str(source), str(destination), overwrite=True) == {
        "ok": True,
        "path": str(destination),
    }
    assert source.read_text(encoding="utf-8") == "new"
    assert destination.read_text(encoding="utf-8") == "new"


def test_copy_directory_overwrite_replaces_complete_tree(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "new.txt").write_text("new", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")

    result = fs.copy_path(str(source), str(destination), overwrite=True)

    assert result == {"ok": True, "path": str(destination)}
    assert not (destination / "old.txt").exists()
    assert (destination / "nested" / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(f"{fs._COPY_TEMP_PREFIX}*"))
    assert not list(tmp_path.glob(".avibe-overwrite-*"))


def test_copy_directory_rejects_own_subtree(tmp_path):
    source = tmp_path / "source"
    child = source / "child"
    child.mkdir(parents=True)

    with pytest.raises(FileBrowserError) as exc:
        fs.copy_path(str(source), str(child / "copy"))

    assert exc.value.code == "invalid_path"
    assert not (child / "copy").exists()


def test_copy_symlinks_as_links_without_reading_targets(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("not copied", encoding="utf-8")
    source_link = tmp_path / "source-link"
    source_link.symlink_to(secret)
    copied_link = tmp_path / "copied-link"

    assert fs.copy_path(str(source_link), str(copied_link)) == {"ok": True, "path": str(copied_link)}
    assert copied_link.is_symlink()
    assert os.readlink(copied_link) == str(secret)

    source_dir = tmp_path / "source-dir"
    source_dir.mkdir()
    (source_dir / "secret-link").symlink_to(secret)
    copied_dir = tmp_path / "copied-dir"
    monkeypatch.setenv(fs._COPY_TOTAL_SIZE_CAP_ENV, "0")

    fs.copy_path(str(source_dir), str(copied_dir))

    assert (copied_dir / "secret-link").is_symlink()
    assert os.readlink(copied_dir / "secret-link") == str(secret)
    assert secret.read_text(encoding="utf-8") == "not copied"


def test_copy_directory_failure_cleans_partial_stage_and_destination(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.txt").write_text("first", encoding="utf-8")
    (source / "second.txt").write_text("second", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "original.txt").write_text("original", encoding="utf-8")
    real_copy = fs._copy_file_from_fd
    calls = {"count": 0}

    def fail_after_copy(*args, **kwargs):
        real_copy(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(errno.EIO, "copy failed")

    monkeypatch.setattr(fs, "_copy_file_from_fd", fail_after_copy)

    with pytest.raises(FileBrowserError) as exc:
        fs.copy_path(str(source), str(destination), overwrite=True)

    assert exc.value.code == "fs_error"
    assert (destination / "original.txt").read_text(encoding="utf-8") == "original"
    assert not (destination / "first.txt").exists()
    assert not (destination / "second.txt").exists()
    assert not list(tmp_path.glob(f"{fs._COPY_TEMP_PREFIX}*"))


def test_copy_cleanup_never_chmods_symlink_target(tmp_path, monkeypatch):
    external = tmp_path / "external.txt"
    external.write_text("external", encoding="utf-8")
    external.chmod(0o600)
    source = tmp_path / "source"
    locked = source / "locked"
    locked.mkdir(parents=True)
    (locked / "external-link").symlink_to(external)
    locked.chmod(0o500)
    destination = tmp_path / "destination"

    def fail_publish(*_args, **_kwargs):
        raise OSError(errno.EIO, "publish failed")

    monkeypatch.setattr(fs, "_publish_staged_copy", fail_publish)

    with pytest.raises(FileBrowserError) as exc:
        fs.copy_path(str(source), str(destination))

    assert exc.value.code == "fs_error"
    assert external.stat().st_mode & 0o777 == 0o600
    assert not destination.exists()
    assert not list(tmp_path.glob(f"{fs._COPY_TEMP_PREFIX}*"))


def test_copy_file_size_cap_rejects_before_staging(tmp_path, monkeypatch):
    source = tmp_path / "large.bin"
    source.write_bytes(b"12345")
    destination = tmp_path / "destination.bin"
    monkeypatch.setenv(fs._COPY_TOTAL_SIZE_CAP_ENV, "4")
    monkeypatch.setattr(
        fs,
        "_new_copy_stage_path",
        lambda _target: (_ for _ in ()).throw(AssertionError("copy should reject before staging")),
    )

    with pytest.raises(FileBrowserError) as exc:
        fs.copy_path(str(source), str(destination))

    assert exc.value.status_code == 413
    assert exc.value.code == "too_large"
    assert not destination.exists()


def test_copy_directory_size_cap_rejects_before_staging(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.bin").write_bytes(b"12345")
    destination = tmp_path / "destination"
    monkeypatch.setenv(fs._COPY_TOTAL_SIZE_CAP_ENV, "4")
    monkeypatch.setattr(
        fs,
        "_new_copy_stage_path",
        lambda _target: (_ for _ in ()).throw(AssertionError("copy should reject before staging")),
    )

    with pytest.raises(FileBrowserError) as exc:
        fs.copy_path(str(source), str(destination))

    assert exc.value.status_code == 413
    assert exc.value.code == "too_large"
    assert not destination.exists()
    assert not list(tmp_path.glob(f"{fs._COPY_TEMP_PREFIX}*"))


@pytest.mark.parametrize(
    ("src", "dst", "expected_code"),
    [
        ("relative-source", "/tmp/destination", "invalid_path"),
        ("/tmp/source", "relative-destination", "invalid_path"),
        ("/tmp/source", "/tmp/parent/..", "invalid_path"),
        ("/tmp/source", "/tmp/bad\\name", "invalid_name"),
    ],
)
def test_copy_rejects_traversal_and_invalid_names(tmp_path, src, dst, expected_code):
    source = tmp_path / "source"
    source.write_text("source", encoding="utf-8")
    parent = tmp_path / "parent"
    parent.mkdir()
    src = str(source) if src == "/tmp/source" else src
    dst = str(parent / "..") if dst == "/tmp/parent/.." else dst
    dst = str(tmp_path / "bad\\name") if dst == "/tmp/bad\\name" else dst

    with pytest.raises(FileBrowserError) as exc:
        fs.copy_path(src, dst)

    assert exc.value.code == expected_code


def test_mutating_ops_mkdir_rename_move_delete(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="core.file_browser_service")
    folder = tmp_path / "folder"
    assert fs.make_directory(str(folder)) == {"ok": True}

    with pytest.raises(FileBrowserError) as exists_exc:
        fs.make_directory(str(folder))
    assert exists_exc.value.code == "exists"

    file_path = folder / "old.txt"
    file_path.write_text("x", encoding="utf-8")
    renamed = fs.rename_path(str(file_path), "new.txt")
    new_path = Path(renamed["path"])
    assert new_path.exists()

    with pytest.raises(FileBrowserError) as invalid_name:
        fs.rename_path(str(new_path), "../bad")
    assert invalid_name.value.code == "invalid_name"

    moved = tmp_path / "moved.txt"
    assert fs.move_path(str(new_path), str(moved)) == {"ok": True}
    assert moved.exists()

    other = tmp_path / "other.txt"
    other.write_text("other", encoding="utf-8")
    with pytest.raises(FileBrowserError) as overwrite_exc:
        fs.move_path(str(moved), str(other))
    assert overwrite_exc.value.code == "exists"
    fs.move_path(str(moved), str(other), overwrite=True)
    assert other.read_text(encoding="utf-8") == "x"

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child", encoding="utf-8")
    with pytest.raises(FileBrowserError) as non_recursive:
        fs.delete_path(str(nested))
    assert non_recursive.value.code == "not_empty"
    fs.delete_path(str(nested), recursive=True)
    assert not nested.exists()
    assert any("file_browser.delete" in record.message for record in caplog.records)


def test_delete_undo_restores_file(files_undo_runtime, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="core.file_browser_service")
    target = tmp_path / "note.txt"
    target.write_text("keep", encoding="utf-8")

    deleted = fs.delete_path(str(target))

    assert deleted["ok"] is True
    assert isinstance(deleted["undo_token"], str)
    assert deleted["undo_expires_seconds"] == fs.DELETE_UNDO_TTL_SECONDS
    assert not target.exists()
    restored = fs.undo_delete_path(deleted["undo_token"])
    assert restored == {"restored_path": str(target)}
    assert target.read_text(encoding="utf-8") == "keep"
    assert any("file_browser.delete_undo" in record.message for record in caplog.records)


def test_delete_undo_restores_directory(files_undo_runtime, tmp_path):
    folder = tmp_path / "folder"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    (nested / "child.txt").write_text("child", encoding="utf-8")

    deleted = fs.delete_path(str(folder), recursive=True)

    assert deleted["undo_token"]
    assert not folder.exists()
    assert fs.undo_delete_path(deleted["undo_token"]) == {"restored_path": str(folder)}
    assert (nested / "child.txt").read_text(encoding="utf-8") == "child"


def test_delete_undo_restores_empty_directory_without_recursive(files_undo_runtime, tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()

    deleted = fs.delete_path(str(folder), recursive=False)

    assert deleted["undo_token"]
    assert not folder.exists()
    assert fs.undo_delete_path(deleted["undo_token"]) == {"restored_path": str(folder)}
    assert folder.is_dir()


def test_delete_non_recursive_directory_created_child_during_staging_returns_not_empty(
    files_undo_runtime, tmp_path, monkeypatch
):
    folder = tmp_path / "folder"
    folder.mkdir()
    real_rename = fs._os_rename_noreplace

    def rename_then_populate(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        if Path(destination).name == fs._DELETE_UNDO_ENTRY_NAME:
            (Path(destination) / "child.txt").write_text("child", encoding="utf-8")

    monkeypatch.setattr(fs, "_os_rename_noreplace", rename_then_populate)

    with pytest.raises(FileBrowserError) as exc:
        fs.delete_path(str(folder), recursive=False)

    assert exc.value.code == "not_empty"
    assert folder.is_dir()
    assert (folder / "child.txt").read_text(encoding="utf-8") == "child"
    undo_root = files_undo_runtime / fs._DELETE_UNDO_DIR_NAME
    assert not undo_root.exists() or not list(undo_root.iterdir())


def test_delete_undo_token_expiry_purges_staging(files_undo_runtime, tmp_path, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(fs.time, "time", lambda: now)
    monkeypatch.setattr(fs, "DELETE_UNDO_TTL_SECONDS", 10)
    target = tmp_path / "expired.txt"
    target.write_text("old", encoding="utf-8")
    token = fs.delete_path(str(target))["undo_token"]

    now = 1011.0
    with pytest.raises(FileBrowserError) as exc:
        fs.undo_delete_path(token)

    assert exc.value.code == "expired"
    assert exc.value.status_code == 410
    assert not (files_undo_runtime / fs._DELETE_UNDO_DIR_NAME / token).exists()


def test_delete_undo_expiry_timer_purges_without_later_file_request(files_undo_runtime, tmp_path, monkeypatch):
    now = 2000.0
    scheduled: list[object] = []

    class FakeTimer:
        def __init__(self, interval, function, args=(), kwargs=None):
            self.interval = interval
            self.function = function
            self.args = args
            self.kwargs = kwargs or {}
            self.started = False
            self.cancelled = False
            scheduled.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    monkeypatch.setattr(fs.time, "time", lambda: now)
    monkeypatch.setattr(fs, "DELETE_UNDO_TTL_SECONDS", 10)
    monkeypatch.setattr(fs.threading, "Timer", FakeTimer)
    target = tmp_path / "scheduled-expiry.txt"
    target.write_text("expire me", encoding="utf-8")

    token = fs.delete_path(str(target))["undo_token"]

    assert scheduled
    assert scheduled[-1].interval == 10
    assert scheduled[-1].started is True
    stage_dir = files_undo_runtime / fs._DELETE_UNDO_DIR_NAME / token
    assert stage_dir.exists()

    now = 2011.0
    scheduled[-1].function(*scheduled[-1].args, **scheduled[-1].kwargs)

    assert not stage_dir.exists()


def test_delete_undo_refuses_to_clobber_recreated_path(files_undo_runtime, tmp_path):
    target = tmp_path / "occupied.txt"
    target.write_text("deleted", encoding="utf-8")
    token = fs.delete_path(str(target))["undo_token"]
    target.write_text("new", encoding="utf-8")

    with pytest.raises(FileBrowserError) as exc:
        fs.undo_delete_path(token)

    assert exc.value.code == "exists"
    assert exc.value.status_code == 409
    assert target.read_text(encoding="utf-8") == "new"
    target.unlink()
    assert fs.undo_delete_path(token) == {"restored_path": str(target)}
    assert target.read_text(encoding="utf-8") == "deleted"


def test_delete_cross_device_staging_falls_back_to_permanent_delete(files_undo_runtime, tmp_path, monkeypatch):
    target = tmp_path / "cross-device.txt"
    target.write_text("delete me", encoding="utf-8")

    def raise_exdev(_source: Path, _target: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(fs, "_os_rename_noreplace", raise_exdev)

    deleted = fs.delete_path(str(target))

    assert deleted == {"ok": True, "undo_token": None, "undo_expires_seconds": None}
    assert not target.exists()


def test_delete_size_cap_falls_back_to_permanent_delete(files_undo_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_FILES_UNDO_SIZE_CAP_BYTES", "3")
    target = tmp_path / "large.txt"
    target.write_bytes(b"1234")

    deleted = fs.delete_path(str(target))

    assert deleted == {"ok": True, "undo_token": None, "undo_expires_seconds": None}
    assert not target.exists()


def test_delete_post_rename_growth_falls_back_to_permanent_delete(files_undo_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_FILES_UNDO_SIZE_CAP_BYTES", "8")
    target = tmp_path / "active.log"
    target.write_text("1234", encoding="utf-8")
    real_rename = fs._os_rename_noreplace

    def rename_then_grow(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        with destination.open("ab") as handle:
            handle.write(b"567890")

    monkeypatch.setattr(fs, "_os_rename_noreplace", rename_then_grow)

    deleted = fs.delete_path(str(target))

    assert deleted == {"ok": True, "undo_token": None, "undo_expires_seconds": None}
    assert not target.exists()
    undo_root = files_undo_runtime / fs._DELETE_UNDO_DIR_NAME
    assert not undo_root.exists() or not list(undo_root.iterdir())


def test_delete_post_rename_growth_reports_cleanup_failure(files_undo_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_FILES_UNDO_SIZE_CAP_BYTES", "8")
    target = tmp_path / "active.log"
    target.write_text("1234", encoding="utf-8")
    real_rename = fs._os_rename_noreplace
    real_remove = fs._delete_undo_remove_path
    failed_removals: list[Path] = []

    def rename_then_grow(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        with destination.open("ab") as handle:
            handle.write(b"567890")

    def fail_stage_cleanup(path: Path) -> bool:
        if Path(path).parent == files_undo_runtime / fs._DELETE_UNDO_DIR_NAME:
            failed_removals.append(Path(path))
            return False
        return real_remove(path)

    monkeypatch.setattr(fs, "_os_rename_noreplace", rename_then_grow)
    monkeypatch.setattr(fs, "_delete_undo_remove_path", fail_stage_cleanup)

    with pytest.raises(FileBrowserError) as exc:
        fs.delete_path(str(target))

    assert exc.value.code == "fs_error"
    assert not target.exists()
    assert failed_removals
    assert failed_removals[0].exists()


def test_delete_stale_file_replaced_by_directory_is_not_staged_or_removed(files_undo_runtime, tmp_path, monkeypatch):
    target = tmp_path / "stale"
    target.write_text("old file", encoding="utf-8")
    real_purge = fs._purge_delete_undo_store_best_effort
    swapped = False

    def swap_file_for_directory_once() -> None:
        nonlocal swapped
        if not swapped:
            target.unlink()
            target.mkdir()
            (target / "child.txt").write_text("new directory", encoding="utf-8")
            swapped = True
        real_purge()

    monkeypatch.setattr(fs, "_purge_delete_undo_store_best_effort", swap_file_for_directory_once)

    with pytest.raises(FileBrowserError) as exc:
        fs.delete_path(str(target), recursive=False)

    assert exc.value.code in {"fs_error", "permission_denied"}
    assert target.is_dir()
    assert (target / "child.txt").read_text(encoding="utf-8") == "new directory"
    undo_root = files_undo_runtime / fs._DELETE_UNDO_DIR_NAME
    assert not undo_root.exists() or not list(undo_root.iterdir())


def test_delete_staging_fsyncs_undo_root_before_returning_token(files_undo_runtime, tmp_path, monkeypatch):
    target = tmp_path / "durable.txt"
    target.write_text("durable", encoding="utf-8")
    fsynced: list[Path] = []
    real_fsync_dir = fs._fsync_dir

    def capture_fsync_dir(path: Path) -> None:
        fsynced.append(Path(path))
        real_fsync_dir(path)

    monkeypatch.setattr(fs, "_fsync_dir", capture_fsync_dir)

    deleted = fs.delete_path(str(target))

    assert deleted["undo_token"]
    assert files_undo_runtime / fs._DELETE_UNDO_DIR_NAME in fsynced


def test_delete_undo_eviction_by_count_and_total_size(files_undo_runtime, tmp_path, monkeypatch):
    now = 2000.0
    monkeypatch.setattr(fs.time, "time", lambda: now)
    monkeypatch.setenv("AVIBE_FILES_UNDO_MAX_ENTRIES", "2")
    tokens: list[str] = []
    for index in range(3):
        now = 2000.0 + index
        target = tmp_path / f"count-{index}.txt"
        target.write_text(str(index), encoding="utf-8")
        tokens.append(fs.delete_path(str(target))["undo_token"])

    with pytest.raises(FileBrowserError) as count_exc:
        fs.undo_delete_path(tokens[0])
    assert count_exc.value.code == "expired"
    assert fs.undo_delete_path(tokens[1]) == {"restored_path": str(tmp_path / "count-1.txt")}
    assert fs.undo_delete_path(tokens[2]) == {"restored_path": str(tmp_path / "count-2.txt")}

    now_total = 3000.0
    monkeypatch.setattr(fs.time, "time", lambda: now_total)
    monkeypatch.setenv("AVIBE_FILES_UNDO_MAX_ENTRIES", "32")
    monkeypatch.setenv("AVIBE_FILES_UNDO_TOTAL_SIZE_CAP_BYTES", "6")
    first = tmp_path / "total-1.txt"
    second = tmp_path / "total-2.txt"
    first.write_text("1234", encoding="utf-8")
    second.write_text("5678", encoding="utf-8")
    first_token = fs.delete_path(str(first))["undo_token"]
    now_total = 3001.0
    second_token = fs.delete_path(str(second))["undo_token"]

    with pytest.raises(FileBrowserError) as total_exc:
        fs.undo_delete_path(first_token)
    assert total_exc.value.code == "expired"
    assert fs.undo_delete_path(second_token) == {"restored_path": str(second)}


def test_delete_undo_failed_eviction_keeps_caps_enforced(files_undo_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_FILES_UNDO_MAX_ENTRIES", "1")
    first = tmp_path / "first.txt"
    first.write_text("first", encoding="utf-8")
    first_token = fs.delete_path(str(first))["undo_token"]
    first_stage_dir = files_undo_runtime / fs._DELETE_UNDO_DIR_NAME / first_token
    real_remove = fs._delete_undo_remove_path

    def fail_first_eviction(path: Path) -> bool:
        if Path(path) == first_stage_dir:
            return False
        return real_remove(path)

    monkeypatch.setattr(fs, "_delete_undo_remove_path", fail_first_eviction)

    second = tmp_path / "second.txt"
    second.write_text("second", encoding="utf-8")
    deleted_second = fs.delete_path(str(second))

    assert deleted_second == {"ok": True, "undo_token": None, "undo_expires_seconds": None}
    assert not second.exists()
    assert first_stage_dir.exists()
    assert fs.undo_delete_path(first_token) == {"restored_path": str(first)}
    assert first.read_text(encoding="utf-8") == "first"


def test_delete_undo_purges_entry_that_grows_past_cap(files_undo_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_FILES_UNDO_SIZE_CAP_BYTES", "8")
    target = tmp_path / "growing.log"
    target.write_text("1234", encoding="utf-8")

    token = fs.delete_path(str(target))["undo_token"]
    staged_entry = files_undo_runtime / fs._DELETE_UNDO_DIR_NAME / token / fs._DELETE_UNDO_ENTRY_NAME
    staged_entry.write_text("123456789", encoding="utf-8")

    with pytest.raises(FileBrowserError) as exc:
        fs.undo_delete_path(token)

    assert exc.value.code == "expired"
    assert not staged_entry.exists()
    assert not target.exists()


def test_delete_stages_symlink_entry_without_touching_target(files_undo_runtime, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    deleted = fs.delete_path(str(link))

    assert deleted["undo_token"]
    assert not link.exists()
    assert target.read_text(encoding="utf-8") == "target"
    assert fs.undo_delete_path(deleted["undo_token"]) == {"restored_path": str(link)}
    assert link.is_symlink()
    assert link.resolve() == target
    assert target.read_text(encoding="utf-8") == "target"


def test_delete_undo_rejects_replaced_parent_symlink(files_undo_runtime, tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "note.txt"
    target.write_text("deleted", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()

    token = fs.delete_path(str(target))["undo_token"]
    parent.rmdir()
    parent.symlink_to(other, target_is_directory=True)

    with pytest.raises(FileBrowserError) as exc:
        fs.undo_delete_path(token)

    assert exc.value.code == "expired"
    assert not (other / "note.txt").exists()
    assert not (files_undo_runtime / fs._DELETE_UNDO_DIR_NAME / token).exists()


def test_delete_undo_parent_open_permission_failure_preserves_token(files_undo_runtime, tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "note.txt"
    target.write_text("deleted", encoding="utf-8")
    token = fs.delete_path(str(target))["undo_token"]
    stage_dir = files_undo_runtime / fs._DELETE_UNDO_DIR_NAME / token
    real_open = fs.os.open

    def fail_parent_open(path, flags, *args, **kwargs):
        if Path(path) == parent:
            raise PermissionError(errno.EACCES, "permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fs.os, "open", fail_parent_open)
        with pytest.raises(FileBrowserError) as exc:
            fs.undo_delete_path(token)

    assert exc.value.code == "permission_denied"
    assert stage_dir.exists()
    assert fs.undo_delete_path(token) == {"restored_path": str(target)}
    assert target.read_text(encoding="utf-8") == "deleted"


def test_delete_undo_restores_through_verified_parent_when_path_is_replaced(
    files_undo_runtime, tmp_path, monkeypatch
):
    parent = tmp_path / "parent"
    parent.mkdir()
    folder = parent / "folder"
    folder.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    detached_parent = tmp_path / "detached-parent"
    token = fs.delete_path(str(folder), recursive=True)["undo_token"]
    real_os_rename = os.rename
    swapped = False

    def renameat2_unavailable(*args, **kwargs):
        raise AttributeError("renameat2 unavailable")

    def replace_parent_then_rename(source, destination, *args, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get("dst_dir_fd") is not None and destination == folder.name:
            swapped = True
            real_os_rename(parent, detached_parent)
            parent.symlink_to(other, target_is_directory=True)
            try:
                return real_os_rename(source, destination, *args, **kwargs)
            finally:
                parent.unlink()
                real_os_rename(detached_parent, parent)
        return real_os_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(fs, "_glibc_renameat2_noreplace_between", renameat2_unavailable)
    monkeypatch.setattr(fs.os, "rename", replace_parent_then_rename)

    assert fs.undo_delete_path(token) == {"restored_path": str(folder)}

    assert swapped is True
    assert folder.is_dir()
    assert not (other / "folder").exists()


def test_delete_undo_purges_corrupt_staging_entries(files_undo_runtime, tmp_path):
    corrupt = files_undo_runtime / fs._DELETE_UNDO_DIR_NAME / ("0" * 32)
    corrupt.mkdir(parents=True)
    (corrupt / fs._DELETE_UNDO_ENTRY_NAME).write_text("orphan", encoding="utf-8")
    (corrupt / fs._DELETE_UNDO_METADATA_NAME).write_text("{not json", encoding="utf-8")
    target = tmp_path / "fresh.txt"
    target.write_text("fresh", encoding="utf-8")

    deleted = fs.delete_path(str(target))

    assert deleted["undo_token"]
    assert not corrupt.exists()


def test_delete_non_recursive_directory_maps_not_empty_only_for_not_empty_errno(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "child.txt").write_text("child", encoding="utf-8")

    with pytest.raises(FileBrowserError) as exc:
        fs.delete_path(str(nested), recursive=False)

    assert exc.value.code == "not_empty"
    assert exc.value.status_code == 409


def test_delete_non_recursive_directory_maps_permission_denied(tmp_path, monkeypatch):
    folder = tmp_path / "folder"
    folder.mkdir()
    monkeypatch.setattr(fs, "_stage_delete_for_undo", lambda *args, **kwargs: None)

    def fail_rmdir(self: Path) -> None:
        if self == folder:
            raise PermissionError(errno.EACCES, "permission denied", str(self))
        return real_rmdir(self)

    real_rmdir = Path.rmdir
    monkeypatch.setattr(Path, "rmdir", fail_rmdir)

    with pytest.raises(FileBrowserError) as exc:
        fs.delete_path(str(folder), recursive=False)

    assert exc.value.code == "permission_denied"
    assert exc.value.status_code == 403


def test_delete_non_recursive_directory_maps_concurrent_missing_to_not_found(tmp_path, monkeypatch):
    folder = tmp_path / "folder"
    folder.mkdir()
    monkeypatch.setattr(fs, "_stage_delete_for_undo", lambda *args, **kwargs: None)

    def fail_rmdir(self: Path) -> None:
        if self == folder:
            raise FileNotFoundError(errno.ENOENT, "missing", str(self))
        return real_rmdir(self)

    real_rmdir = Path.rmdir
    monkeypatch.setattr(Path, "rmdir", fail_rmdir)

    with pytest.raises(FileBrowserError) as exc:
        fs.delete_path(str(folder), recursive=False)

    assert exc.value.code == "not_found"
    assert exc.value.status_code == 404


def test_delete_non_recursive_directory_maps_generic_oserror_to_fs_error(tmp_path, monkeypatch):
    folder = tmp_path / "folder"
    folder.mkdir()
    monkeypatch.setattr(fs, "_stage_delete_for_undo", lambda *args, **kwargs: None)

    def fail_rmdir(self: Path) -> None:
        if self == folder:
            raise OSError(errno.EIO, "io error", str(self))
        return real_rmdir(self)

    real_rmdir = Path.rmdir
    monkeypatch.setattr(Path, "rmdir", fail_rmdir)

    with pytest.raises(FileBrowserError) as exc:
        fs.delete_path(str(folder), recursive=False)

    assert exc.value.code == "fs_error"
    assert exc.value.status_code == 400


def test_move_overwrite_restores_destination_when_move_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")

    def fail_move(_src: Path, _dst: Path, *, on_target_placed=None) -> None:
        raise OSError("simulated cross-device failure")

    monkeypatch.setattr(fs, "_move_to_absent_target", fail_move)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=True)

    assert exc.value.code == "fs_error"
    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "destination"
    assert not list(tmp_path.glob(".destination.txt.avibe-overwrite-*"))


def test_move_overwrite_uses_bounded_backup_name_for_long_destination(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    long_name = "d" * 255
    destination = tmp_path / long_name
    destination.write_text("destination", encoding="utf-8")
    reserved: list[Path] = []
    real_reserve_backup_path = fs._reserve_backup_path

    def capture_reserve(target: Path) -> Path:
        backup = real_reserve_backup_path(target)
        reserved.append(backup)
        return backup

    monkeypatch.setattr(fs, "_reserve_backup_path", capture_reserve)

    assert fs.move_path(str(source), str(destination), overwrite=True) == {"ok": True}

    assert destination.read_text(encoding="utf-8") == "source"
    assert len(reserved) == 1
    assert reserved[0].parent == tmp_path
    assert reserved[0].name.startswith(".avibe-overwrite-")
    assert len(reserved[0].name) < 64
    assert long_name not in reserved[0].name
    assert not reserved[0].exists()


def test_move_symlink_onto_its_target_is_refused(tmp_path):
    real = tmp_path / "real.txt"
    real.write_bytes(b"original bytes")
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(link), str(real), overwrite=True)

    assert exc.value.code == "invalid_move"
    assert real.read_bytes() == b"original bytes"
    assert real.is_file()
    assert not real.is_symlink()
    assert link.is_symlink()

    other_real = tmp_path / "other-real.txt"
    other_real.write_text("other", encoding="utf-8")
    other_link = tmp_path / "other-link"
    other_link.symlink_to(other_real)
    destination = tmp_path / "destination.txt"
    destination.write_text("destination", encoding="utf-8")

    assert fs.move_path(str(other_link), str(destination), overwrite=True) == {"ok": True}
    assert destination.is_symlink()
    assert destination.resolve() == other_real
    assert other_real.read_text(encoding="utf-8") == "other"


def test_move_directory_into_own_descendant_is_refused_before_copy(tmp_path, monkeypatch):
    source = tmp_path / "a"
    destination_parent = source / "b"
    destination = destination_parent / "c"
    destination_parent.mkdir(parents=True)
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    copytree_calls: list[tuple[Path, Path]] = []

    def fail_copytree(src: Path, dst: Path, **_kwargs):
        copytree_calls.append((Path(src), Path(dst)))
        raise AssertionError("copytree should not run for an invalid self-descendant move")

    monkeypatch.setattr(fs.shutil, "copytree", fail_copytree)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination))

    assert exc.value.code == "invalid_path"
    assert exc.value.status_code == 400
    assert source.is_dir()
    assert (source / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert copytree_calls == []


def test_move_directory_to_sibling_still_succeeds(tmp_path):
    source = tmp_path / "a"
    source.mkdir()
    (source / "keep.txt").write_text("keep", encoding="utf-8")
    destination = tmp_path / "d"

    assert fs.move_path(str(source), str(destination)) == {"ok": True}
    assert not source.exists()
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_move_directory_onto_itself_is_noop(tmp_path):
    # An exact self-move stays an idempotent no-op — the into-itself guard must only reject
    # moves into a descendant, not a move onto the source's own path.
    folder = tmp_path / "a"
    folder.mkdir()
    (folder / "keep.txt").write_text("keep", encoding="utf-8")

    assert fs.move_path(str(folder), str(folder)) == {"ok": True}
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_move_no_overwrite_refuses_target_appearing_after_precheck(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")

    real_exists = fs._exists_no_follow
    monkeypatch.setattr(fs, "_exists_no_follow", lambda p: False if Path(p) == destination else real_exists(p))

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=False)

    assert exc.value.code == "exists"
    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "destination"


def test_move_no_overwrite_hard_link_fallback_rolls_back_when_unlink_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")

    monkeypatch.setattr(fs, "_glibc_renameat2_noreplace", lambda _src, _dst: (_ for _ in ()).throw(AttributeError()))
    real_unlink = fs.os.unlink

    def fail_source_unlink(path, *args, **kwargs):
        if Path(path) == source:
            raise OSError("cannot unlink source")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(fs.os, "unlink", fail_source_unlink)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=False)

    assert exc.value.code == "fs_error"
    assert source.exists()
    assert source.read_text(encoding="utf-8") == "source"
    assert not destination.exists()


def test_move_no_overwrite_hard_link_fallback_still_moves(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")

    monkeypatch.setattr(fs, "_glibc_renameat2_noreplace", lambda _src, _dst: (_ for _ in ()).throw(AttributeError()))

    assert fs.move_path(str(source), str(destination), overwrite=False) == {"ok": True}
    assert destination.read_text(encoding="utf-8") == "source"
    assert not source.exists()


def test_move_cross_filesystem_copies_then_removes_source(tmp_path, monkeypatch):
    # A cross-filesystem move raises EXDEV from the no-replace rename; the move must then
    # copy the source to a destination-side temp, atomically place it (still no-replace),
    # and only then remove the original source — never lose data.
    source = tmp_path / "src.txt"
    source.write_text("DATA", encoding="utf-8")
    destination = tmp_path / "dst.txt"

    real_rename = fs._os_rename_noreplace
    calls = {"n": 0}

    def fake_rename(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst)  # temp -> destination succeeds within one filesystem

    monkeypatch.setattr(fs, "_os_rename_noreplace", fake_rename)

    assert fs.move_path(str(source), str(destination), overwrite=False) == {"ok": True}
    assert destination.read_text(encoding="utf-8") == "DATA"
    assert not source.exists()
    # No overwrite-temp siblings left behind.
    assert not list(tmp_path.glob(".dst.txt.avibe-overwrite-*"))


def test_move_cross_filesystem_rolls_back_target_when_source_removal_fails(tmp_path, monkeypatch):
    source = tmp_path / "src.txt"
    source.write_text("DATA", encoding="utf-8")
    destination = tmp_path / "dst.txt"

    real_rename = fs._os_rename_noreplace
    calls = {"n": 0}

    def fake_rename(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst)

    real_remove_backup_path = fs._remove_backup_path

    def fail_source_removal(path: Path) -> None:
        if Path(path) == source:
            raise PermissionError(errno.EACCES, "permission denied", str(path))
        real_remove_backup_path(path)

    monkeypatch.setattr(fs, "_os_rename_noreplace", fake_rename)
    monkeypatch.setattr(fs, "_remove_backup_path", fail_source_removal)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=False)

    assert exc.value.code == "fs_error"
    assert source.read_text(encoding="utf-8") == "DATA"
    assert not destination.exists()
    assert not list(tmp_path.glob(".dst.txt.avibe-overwrite-*"))


def test_move_cross_filesystem_directory_keeps_target_when_source_removal_partially_fails(tmp_path, monkeypatch):
    source = tmp_path / "src"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "keep.txt").write_text("complete", encoding="utf-8")
    (source / "partial.txt").write_text("will be removed first", encoding="utf-8")
    destination = tmp_path / "dst"

    real_rename = fs._os_rename_noreplace
    calls = {"n": 0}

    def fake_rename(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst)

    real_remove_backup_path = fs._remove_backup_path

    def fail_source_removal(path: Path) -> None:
        if Path(path) == source:
            (source / "partial.txt").unlink()
            raise PermissionError(errno.EACCES, "permission denied", str(path))
        real_remove_backup_path(path)

    monkeypatch.setattr(fs, "_os_rename_noreplace", fake_rename)
    monkeypatch.setattr(fs, "_remove_backup_path", fail_source_removal)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=False)

    assert exc.value.code == "fs_error"
    assert not (source / "partial.txt").exists()
    assert (destination / "partial.txt").read_text(encoding="utf-8") == "will be removed first"
    assert (destination / "nested" / "keep.txt").read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".avibe-overwrite-*"))


def test_move_cross_filesystem_directory_still_moves(tmp_path, monkeypatch):
    source = tmp_path / "src"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "keep.txt").write_text("complete", encoding="utf-8")
    destination = tmp_path / "dst"

    real_rename = fs._os_rename_noreplace
    calls = {"n": 0}

    def fake_rename(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(fs, "_os_rename_noreplace", fake_rename)

    assert fs.move_path(str(source), str(destination), overwrite=False) == {"ok": True}
    assert not source.exists()
    assert (destination / "nested" / "keep.txt").read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".avibe-overwrite-*"))


def test_move_overwrite_absent_cross_filesystem_still_moves(tmp_path, monkeypatch):
    source = tmp_path / "src.txt"
    source.write_text("DATA", encoding="utf-8")
    destination = tmp_path / "dst.txt"

    real_rename = fs._os_rename_noreplace
    calls = {"n": 0}

    def fake_rename(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(fs, "_os_rename_noreplace", fake_rename)

    assert fs.move_path(str(source), str(destination), overwrite=True) == {"ok": True}
    assert destination.read_text(encoding="utf-8") == "DATA"
    assert not source.exists()
    assert not list(tmp_path.glob(".dst.txt.avibe-overwrite-*"))


def test_move_overwrite_absent_cross_filesystem_rolls_back_target_when_source_removal_fails(
    tmp_path, monkeypatch
):
    source = tmp_path / "src.txt"
    source.write_text("DATA", encoding="utf-8")
    destination = tmp_path / "dst.txt"

    real_rename = fs._os_rename_noreplace
    calls = {"n": 0}

    def fake_rename(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst)

    real_remove_backup_path = fs._remove_backup_path

    def fail_source_removal(path: Path) -> None:
        if Path(path) == source:
            raise PermissionError(errno.EACCES, "permission denied", str(path))
        real_remove_backup_path(path)

    monkeypatch.setattr(fs, "_os_rename_noreplace", fake_rename)
    monkeypatch.setattr(fs, "_remove_backup_path", fail_source_removal)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=True)

    assert exc.value.code == "fs_error"
    assert source.read_text(encoding="utf-8") == "DATA"
    assert not destination.exists()
    assert not list(tmp_path.glob(".dst.txt.avibe-overwrite-*"))


def test_move_overwrite_cross_filesystem_file_restores_backup_when_source_unlink_fails(tmp_path, monkeypatch):
    source = tmp_path / "src.txt"
    source.write_text("DATA", encoding="utf-8")
    destination = tmp_path / "dst.txt"
    destination.write_text("old destination", encoding="utf-8")

    real_rename = fs._os_rename_noreplace
    calls = {"n": 0}

    def fake_rename(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst)

    real_remove_backup_path = fs._remove_backup_path

    def fail_source_removal(path: Path) -> None:
        if Path(path) == source:
            raise PermissionError(errno.EACCES, "permission denied", str(path))
        real_remove_backup_path(path)

    monkeypatch.setattr(fs, "_os_rename_noreplace", fake_rename)
    monkeypatch.setattr(fs, "_remove_backup_path", fail_source_removal)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=True)

    assert exc.value.code == "fs_error"
    assert source.read_text(encoding="utf-8") == "DATA"
    assert destination.read_text(encoding="utf-8") == "old destination"
    assert not list(tmp_path.glob(".avibe-overwrite-*"))


def test_move_overwrite_preserves_foreign_target_created_after_backup(tmp_path, monkeypatch):
    source = tmp_path / "src.txt"
    source.write_text("source", encoding="utf-8")
    destination = tmp_path / "dst.txt"
    destination.write_text("old destination", encoding="utf-8")

    def foreign_target_conflict(src: Path, dst: Path, *, on_target_placed=None) -> None:
        destination.write_text("foreign target", encoding="utf-8")
        raise fs.ConflictError("exists", "Destination already exists")

    monkeypatch.setattr(fs, "_move_to_absent_target", foreign_target_conflict)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=True)

    assert exc.value.code == "exists"
    assert destination.read_text(encoding="utf-8") == "foreign target"
    backups = list(tmp_path.glob(".avibe-overwrite-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old destination"
    assert source.read_text(encoding="utf-8") == "source"


def test_move_overwrite_restores_backup_when_move_fails_before_target_placed(tmp_path, monkeypatch):
    source = tmp_path / "src.txt"
    source.write_text("source", encoding="utf-8")
    destination = tmp_path / "dst.txt"
    destination.write_text("old destination", encoding="utf-8")

    def fail_before_target_placed(src: Path, dst: Path, *, on_target_placed=None) -> None:
        raise PermissionError(errno.EACCES, "permission denied", str(dst))

    monkeypatch.setattr(fs, "_move_to_absent_target", fail_before_target_placed)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=True)

    assert exc.value.code == "fs_error"
    assert destination.read_text(encoding="utf-8") == "old destination"
    assert source.read_text(encoding="utf-8") == "source"
    assert not list(tmp_path.glob(".avibe-overwrite-*"))


def test_move_overwrite_cross_filesystem_directory_keeps_target_when_source_removal_partially_fails(
    tmp_path, monkeypatch
):
    source = tmp_path / "src"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "keep.txt").write_text("complete", encoding="utf-8")
    (source / "partial.txt").write_text("will be removed first", encoding="utf-8")
    destination = tmp_path / "dst"
    destination.mkdir()
    (destination / "old.txt").write_text("old destination", encoding="utf-8")

    real_rename = fs._os_rename_noreplace
    calls = {"n": 0}

    def fake_rename(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst)

    real_remove_backup_path = fs._remove_backup_path

    def fail_source_removal(path: Path) -> None:
        if Path(path) == source:
            (source / "partial.txt").unlink()
            raise PermissionError(errno.EACCES, "permission denied", str(path))
        real_remove_backup_path(path)

    monkeypatch.setattr(fs, "_os_rename_noreplace", fake_rename)
    monkeypatch.setattr(fs, "_remove_backup_path", fail_source_removal)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=True)

    assert exc.value.code == "fs_error"
    assert not (source / "partial.txt").exists()
    assert not (destination / "old.txt").exists()
    assert (destination / "partial.txt").read_text(encoding="utf-8") == "will be removed first"
    assert (destination / "nested" / "keep.txt").read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".avibe-overwrite-*"))


def test_symlink_mutations_operate_on_link_not_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    renamed = fs.rename_path(str(link), "renamed.txt")
    renamed_link = Path(renamed["path"])
    assert renamed_link.is_symlink()
    assert target.read_text(encoding="utf-8") == "target"

    fs.delete_path(str(renamed_link))
    assert not renamed_link.exists()
    assert target.read_text(encoding="utf-8") == "target"


def test_http_routes_return_contract_and_headers(tmp_path):
    file_path = tmp_path / "note.txt"
    file_path.write_text("hello", encoding="utf-8")
    client = app.test_client()

    list_response = client.get(f"/api/files/list?path={tmp_path}&show_hidden=0")
    assert list_response.status_code == 200
    assert list_response.get_json()["entries"][0]["name"] == "note.txt"

    meta_response = client.get(f"/api/files/meta?path={file_path}")
    assert meta_response.get_json()["mime"] == "text/plain"

    content_response = client.get(f"/api/files/content?path={file_path}")
    assert content_response.status_code == 200
    assert content_response.content == b"hello"
    assert content_response.headers["X-Content-Type-Options"] == "nosniff"
    assert content_response.headers["Content-Disposition"].startswith("inline;")

    download_response = client.get(f"/api/files/content?path={file_path}&download=1")
    assert download_response.headers["Content-Disposition"].startswith("attachment;")

    upload_response = client.post(
        "/api/files/upload",
        data={"dir": str(tmp_path), "name": "uploaded.bin"},
        files={"file": ("browser-name.bin", b"upload", "application/octet-stream")},
        headers=csrf_headers(client),
    )
    assert upload_response.status_code == 200
    upload_body = upload_response.get_json()
    assert set(upload_body) == {"name", "path", "size", "mtime"}
    assert upload_body["name"] == "uploaded.bin"
    assert upload_body["size"] == 6
    assert (tmp_path / "uploaded.bin").read_bytes() == b"upload"


def test_http_copy_returns_contract_and_maps_name_clash(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    destination = tmp_path / "destination.txt"
    client = app.test_client()
    headers = csrf_headers(client)

    response = client.post(
        "/api/files/copy",
        json={"src": str(source), "dst": str(destination)},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "path": str(destination)}
    assert destination.read_text(encoding="utf-8") == "source"

    source.write_text("updated", encoding="utf-8")
    conflict = client.post(
        "/api/files/copy",
        json={"src": str(source), "dst": str(destination), "overwrite": "false"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.get_json() == {
        "ok": False,
        "error": {"code": "exists", "message": "Destination already exists"},
    }
    assert destination.read_text(encoding="utf-8") == "source"

    replaced = client.post(
        "/api/files/copy",
        json={"src": str(source), "dst": str(destination), "overwrite": True},
        headers=headers,
    )
    assert replaced.status_code == 200
    assert replaced.get_json() == {"ok": True, "path": str(destination)}
    assert destination.read_text(encoding="utf-8") == "updated"


def test_http_copy_enforces_csrf(tmp_path):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")
    client = app.test_client()

    response = client.post(
        "/api/files/copy",
        json={"src": str(source), "dst": str(destination)},
    )

    assert response.status_code == 403
    assert not destination.exists()


def test_http_upload_maps_too_large_and_leaves_no_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_FILE_BYTES", 4)
    client = app.test_client()

    response = client.post(
        "/api/files/upload",
        data={"dir": str(tmp_path)},
        files={"file": ("large.bin", b"12345", "application/octet-stream")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 413
    assert response.get_json() == {
        "ok": False,
        "error": {"code": "too_large", "message": "File is too large"},
    }
    assert not (tmp_path / "large.bin").exists()
    assert not list(tmp_path.glob(f"{fs._WRITE_TEMP_PREFIX}*.tmp"))


def test_http_upload_rejects_oversized_content_length_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_FILE_BYTES", 4)
    monkeypatch.setattr(ui_server, "_FILE_UPLOAD_MULTIPART_OVERHEAD_BYTES", 0)

    def boom_upload(*_args, **_kwargs):
        raise AssertionError("oversized upload should be rejected before form parsing")

    monkeypatch.setattr(fs, "upload_file", boom_upload)
    client = app.test_client()

    response = client.post(
        "/api/files/upload",
        data={"dir": str(tmp_path)},
        files={"file": ("large.bin", b"12345", "application/octet-stream")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "too_large"
    assert not (tmp_path / "large.bin").exists()


def test_http_upload_allows_missing_content_length():
    ui_server._validate_file_upload_content_length({}, 4)


def test_http_upload_parser_caps_file_part_before_service(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "MAX_FILE_BYTES", 4)

    def boom_upload(*_args, **_kwargs):
        raise AssertionError("oversized multipart file should be rejected before service write")

    monkeypatch.setattr(fs, "upload_file", boom_upload)
    client = app.test_client()

    response = client.post(
        "/api/files/upload",
        data={"dir": str(tmp_path)},
        files={"file": ("large.bin", b"12345", "application/octet-stream")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "too_large"
    assert not (tmp_path / "large.bin").exists()


def test_http_upload_missing_file_part_returns_structured_error(tmp_path):
    client = app.test_client()

    response = client.post(
        "/api/files/upload",
        data={"dir": str(tmp_path)},
        headers=csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "error": {"code": "invalid_name", "message": "File is required"},
    }


def test_http_upload_overwrite_string_true_replaces_target(tmp_path):
    target = tmp_path / "replace.bin"
    target.write_bytes(b"old")
    client = app.test_client()

    response = client.post(
        "/api/files/upload",
        data={"dir": str(tmp_path), "overwrite": "true"},
        files={"file": ("replace.bin", b"new", "application/octet-stream")},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["name"] == "replace.bin"
    assert target.read_bytes() == b"new"


def test_http_routes_map_structured_errors_and_enforce_csrf(tmp_path):
    client = app.test_client()
    missing = client.get(f"/api/files/meta?path={tmp_path / 'missing.txt'}")
    assert missing.status_code == 404
    assert missing.get_json() == {
        "ok": False,
        "error": {"code": "not_found", "message": "Path not found"},
    }

    write_path = tmp_path / "new.txt"
    blocked = client.put("/api/files/write", json={"path": str(write_path), "content": "x"})
    assert blocked.status_code == 403

    upload_path = tmp_path / "blocked-upload.bin"
    blocked_upload = client.post(
        "/api/files/upload",
        data={"dir": str(tmp_path)},
        files={"file": ("blocked-upload.bin", b"x", "application/octet-stream")},
    )
    assert blocked_upload.status_code == 403
    assert not upload_path.exists()

    ok = client.put(
        "/api/files/write",
        json={"path": str(write_path), "content": "x"},
        headers=csrf_headers(client),
    )
    assert ok.status_code == 200
    assert ok.get_json()["ok"] is True
    assert write_path.read_text(encoding="utf-8") == "x"


def test_http_delete_and_move_string_false_flags_are_not_truthy(tmp_path):
    client = app.test_client()
    headers = csrf_headers(client)

    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "child.txt").write_text("child", encoding="utf-8")
    delete_response = client.post(
        "/api/files/delete",
        json={"path": str(folder), "recursive": "false"},
        headers=headers,
    )
    assert delete_response.status_code == 409
    assert folder.exists()

    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")
    move_response = client.post(
        "/api/files/move",
        json={"src": str(source), "dst": str(destination), "overwrite": "false"},
        headers=headers,
    )
    assert move_response.status_code == 409
    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "destination"


def test_http_delete_response_adds_undo_fields_without_dropping_ok(files_undo_runtime, tmp_path):
    client = app.test_client()
    headers = csrf_headers(client)
    target = tmp_path / "delete-me.txt"
    target.write_text("delete me", encoding="utf-8")

    response = client.post(
        "/api/files/delete",
        json={"path": str(target)},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert isinstance(payload["undo_token"], str)
    assert payload["undo_expires_seconds"] == fs.DELETE_UNDO_TTL_SECONDS

    undo_response = client.post(
        "/api/files/delete/undo",
        json={"token": payload["undo_token"]},
        headers=headers,
    )
    assert undo_response.status_code == 200
    assert undo_response.get_json() == {"restored_path": str(target)}
    assert target.read_text(encoding="utf-8") == "delete me"

    expired = client.post(
        "/api/files/delete/undo",
        json={"token": payload["undo_token"]},
        headers=headers,
    )
    assert expired.status_code == 410
    assert expired.get_json()["error"]["code"] == "expired"


def test_startup_reconcile_skips_tmux_when_env_set(monkeypatch):
    from vibe import api

    monkeypatch.setattr(api, "ensure_askill_installed", lambda force=False: {"ok": True, "installed": True})
    monkeypatch.setattr(api, "ensure_avault_installed", lambda force=False: {"ok": True, "installed": True})
    monkeypatch.setenv("VIBE_UI_ENABLE_TERMINAL", "1")

    import core.show_runtime as srt_mod
    import core.tmux_runtime as tmux_mod

    class _Mgr:
        def status(self):
            return {"installed": False, "node_available": False, "node_version": None}

    monkeypatch.setattr(srt_mod, "get_show_runtime_manager", lambda: _Mgr())

    calls = []
    monkeypatch.delenv("VIBE_INSTALL_SKIP_TMUX", raising=False)
    monkeypatch.setattr(tmux_mod, "ensure_tmux_installed", lambda force=False: calls.append(force) or {"ok": True})
    out_without_skip = api.reconcile_startup_dependencies()
    assert out_without_skip["tmux"] == {"ok": True}
    assert calls == [False]

    monkeypatch.setenv("VIBE_INSTALL_SKIP_TMUX", "yes")
    monkeypatch.setattr(tmux_mod, "ensure_tmux_installed", lambda force=False: pytest.fail("tmux install should be skipped"))
    out_with_skip = api.reconcile_startup_dependencies()

    assert out_with_skip["tmux"] == {"ok": True, "skipped": True, "reason": "VIBE_INSTALL_SKIP_TMUX"}


def test_metadata_text_sniff_distinguishes_text_from_binary(tmp_path):
    # Extensionless text file → text=True, so the editor opens it instead of downloading.
    license_file = tmp_path / "LICENSE"
    license_file.write_text("MIT License\n\nPermission is hereby granted, free of charge...\n", encoding="utf-8")
    assert fs.metadata(str(license_file))["text"] is True

    # An empty file is editable.
    empty = tmp_path / "notes"
    empty.write_bytes(b"")
    assert fs.metadata(str(empty))["text"] is True

    # Binary content (NUL bytes) → not text → stays on the download path.
    blob = tmp_path / "image"  # no extension, but binary
    blob.write_bytes(b"\x89PNG\r\n\x00\x00\x00\rIHDR" + bytes(64))
    assert fs.metadata(str(blob))["text"] is False

    # A known-extension text file still reports text.
    code = tmp_path / "script.py"
    code.write_text("print('hi')\n", encoding="utf-8")
    assert fs.metadata(str(code))["text"] is True

    # Directories carry no text flag.
    sub = tmp_path / "sub"
    sub.mkdir()
    assert fs.metadata(str(sub))["text"] is False
