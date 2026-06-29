from __future__ import annotations

from pathlib import Path

import pytest

from core import file_browser_service as fbs
from core.file_browser_service import FileBrowserError


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rels(result: dict) -> set[str]:
    return {entry["rel"] for entry in result["results"]}


def test_search_literal_groups_by_file_with_positions(tmp_path):
    _write(tmp_path, "a.txt", "alpha onResize beta\nno hit here\nonResize again\n")
    _write(tmp_path, "sub/b.txt", "leading onResize trailing\n")

    result = fbs.search(str(tmp_path), "onResize")

    assert _rels(result) == {"a.txt", "sub/b.txt"}
    assert result["total_matches"] == 3
    assert result["total_files"] == 2
    assert result["truncated"] is False
    a = next(e for e in result["results"] if e["rel"] == "a.txt")
    first = a["matches"][0]
    assert first["line"] == 1
    assert first["col"] == 6
    assert first["end"] == 14
    assert first["text"] == "alpha onResize beta"


def test_search_case_sensitivity(tmp_path):
    _write(tmp_path, "a.txt", "Cat cat CAT\n")
    assert fbs.search(str(tmp_path), "cat")["total_matches"] == 3
    assert fbs.search(str(tmp_path), "cat", case_sensitive=True)["total_matches"] == 1


def test_search_whole_word(tmp_path):
    _write(tmp_path, "a.txt", "cat category scatter cat\n")
    assert fbs.search(str(tmp_path), "cat", whole_word=True)["total_matches"] == 2


def test_search_regex_and_invalid_regex(tmp_path):
    _write(tmp_path, "a.txt", "x1 x22 x333\n")
    assert fbs.search(str(tmp_path), r"x\d+", regex=True)["total_matches"] == 3
    with pytest.raises(FileBrowserError) as exc:
        fbs.search(str(tmp_path), "x(", regex=True)
    assert exc.value.code == "invalid_regex"


def test_search_empty_query_rejected(tmp_path):
    _write(tmp_path, "a.txt", "anything\n")
    with pytest.raises(FileBrowserError) as exc:
        fbs.search(str(tmp_path), "")
    assert exc.value.code == "invalid_query"


def test_search_include_exclude_globs(tmp_path):
    _write(tmp_path, "keep.py", "needle\n")
    _write(tmp_path, "skip.txt", "needle\n")
    _write(tmp_path, "vendor/dep.py", "needle\n")

    assert _rels(fbs.search(str(tmp_path), "needle", include="*.py")) == {"keep.py", "vendor/dep.py"}
    assert _rels(fbs.search(str(tmp_path), "needle", exclude="vendor/**")) == {"keep.py", "skip.txt"}


def test_search_prunes_default_noise_dirs(tmp_path):
    _write(tmp_path, "src.py", "needle\n")
    _write(tmp_path, "node_modules/pkg/index.js", "needle\n")
    _write(tmp_path, ".git/config", "needle\n")
    assert _rels(fbs.search(str(tmp_path), "needle")) == {"src.py"}


def test_search_skips_binary_and_oversized(tmp_path, monkeypatch):
    (tmp_path / "bin.dat").write_bytes(b"needle\x00needle")
    _write(tmp_path, "big.txt", "needle\n" + "x" * 100)
    _write(tmp_path, "small.txt", "needle\n")
    monkeypatch.setattr(fbs, "SEARCH_MAX_FILE_BYTES", 16)
    assert _rels(fbs.search(str(tmp_path), "needle")) == {"small.txt"}


def test_search_truncates_on_match_cap(tmp_path):
    _write(tmp_path, "a.txt", "hit\n" * 50)
    result = fbs.search(str(tmp_path), "hit", max_matches=10)
    assert result["total_matches"] == 10
    assert result["truncated"] is True
    assert result["truncated_reason"] == "matches"


def test_search_truncates_on_file_cap(tmp_path):
    for i in range(5):
        _write(tmp_path, f"f{i}.txt", "hit\n")
    result = fbs.search(str(tmp_path), "hit", max_files=2)
    assert result["truncated"] is True
    assert result["truncated_reason"] == "files"
    assert result["total_files"] == 2


def test_replace_literal_then_undo_roundtrip(tmp_path):
    a = _write(tmp_path, "a.txt", "onResize here\nonResize there\n")
    b = _write(tmp_path, "b.txt", "no match\n")

    result = fbs.replace(str(tmp_path), "onResize", "onWindowResize")
    assert result["total_replacements"] == 2
    assert result["files_changed"] == 1
    assert a.read_text() == "onWindowResize here\nonWindowResize there\n"
    assert b.read_text() == "no match\n"

    token = result["undo_token"]
    assert token
    undo = fbs.undo_replace(token)
    assert undo["restored"] == [str(a)]
    assert a.read_text() == "onResize here\nonResize there\n"

    # token is single-use
    with pytest.raises(FileBrowserError) as exc:
        fbs.undo_replace(token)
    assert exc.value.code == "undo_unavailable"


def test_replace_regex_uses_backrefs_literal_does_not(tmp_path):
    a = _write(tmp_path, "a.txt", "value=42\n")
    fbs.replace(str(tmp_path), r"value=(\d+)", r"v(\1)", regex=True)
    assert a.read_text() == "v(42)\n"

    b = _write(tmp_path, "b.txt", "value=42\n")
    fbs.replace(str(tmp_path), "value=42", r"x\1y")
    assert b.read_text() == r"x\1y" + "\n"


def test_undo_skips_files_modified_after_replace(tmp_path):
    a = _write(tmp_path, "a.txt", "onResize\n")
    result = fbs.replace(str(tmp_path), "onResize", "renamed")
    a.write_text("user edited this after replace\n", encoding="utf-8")

    undo = fbs.undo_replace(result["undo_token"])
    assert undo["restored"] == []
    assert undo["skipped"] == [{"path": str(a), "reason": "modified"}]
    assert a.read_text() == "user edited this after replace\n"


def test_search_root_must_be_directory(tmp_path):
    f = _write(tmp_path, "a.txt", "x\n")
    with pytest.raises(FileBrowserError):
        fbs.search(str(f), "x")
