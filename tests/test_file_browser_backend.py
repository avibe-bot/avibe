from __future__ import annotations

import os
import logging
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
