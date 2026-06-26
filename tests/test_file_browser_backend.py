from __future__ import annotations

import errno
import os
import logging
import sys
from pathlib import Path

import pytest

from core import file_browser_service as fs
from core.file_browser_service import FileBrowserError
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


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


def test_write_preserves_existing_file_mode(tmp_path):
    path = tmp_path / "script.sh"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)

    fs.write_file(str(path), "#!/bin/sh\necho ok\n")

    assert path.read_text(encoding="utf-8") == "#!/bin/sh\necho ok\n"
    assert path.stat().st_mode & 0o777 == 0o755


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


def test_move_overwrite_restores_destination_when_move_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")

    def fail_move(_src: str, _dst: str) -> None:
        raise OSError("simulated cross-device failure")

    monkeypatch.setattr(fs.shutil, "move", fail_move)

    with pytest.raises(FileBrowserError) as exc:
        fs.move_path(str(source), str(destination), overwrite=True)

    assert exc.value.code == "fs_error"
    assert source.read_text(encoding="utf-8") == "source"
    assert destination.read_text(encoding="utf-8") == "destination"
    assert not list(tmp_path.glob(".destination.txt.avibe-overwrite-*"))


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
