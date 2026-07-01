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


def test_whole_word_matches_symbol_query(tmp_path):
    # A symbol ending in a non-word char (C++) must still match under Whole Word — the old both-sides
    # \b made it never match.
    _write(tmp_path, "a.txt", "use C++ here\nand Cxx too\n")
    res = fbs.search(str(tmp_path), "C++", whole_word=True)
    assert res["total_matches"] == 1


def test_include_globstar_matches_root_level(tmp_path):
    _write(tmp_path, "foo.py", "needle\n")
    _write(tmp_path, "sub/bar.py", "needle\n")
    _write(tmp_path, "baz.txt", "needle\n")
    assert _rels(fbs.search(str(tmp_path), "needle", include="**/*.py")) == {"foo.py", "sub/bar.py"}


def test_exclude_globstar_matches_root_level(tmp_path):
    _write(tmp_path, "keep.txt", "needle\n")
    _write(tmp_path, "generated/a.ts", "needle\n")
    assert _rels(fbs.search(str(tmp_path), "needle", exclude="**/generated/**")) == {"keep.txt"}


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


def test_search_skips_symlink_files(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.txt").write_text("needle here\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("needle secret\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    # The symlink must not be followed — only the real in-root file surfaces, and the outside
    # target's contents are never exposed under the root.
    assert _rels(fbs.search(str(root), "needle")) == {"real.txt"}


def test_search_returns_utf16_offsets(tmp_path):
    # 😀 is one code point but two UTF-16 code units; col/end must be UTF-16 so the JS preview
    # slice and Monaco selection line up.
    _write(tmp_path, "e.txt", "😀 onResize\n")
    m = fbs.search(str(tmp_path), "onResize")["results"][0]["matches"][0]
    assert (m["col"], m["end"]) == (3, 11)


def test_replace_partial_failure_preserves_undo(tmp_path, monkeypatch):
    a = _write(tmp_path, "a.txt", "x\n")
    b = _write(tmp_path, "b.txt", "x\n")
    real_write = fbs.write_file

    def flaky(path, content, **kwargs):
        if path.endswith("b.txt"):
            raise FileBrowserError("permission_denied", "denied", 403)
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(fbs, "write_file", flaky)
    res = fbs.replace(str(tmp_path), "x", "y")
    # b.txt failing must not abort the batch or strip undo from a.txt.
    assert res["files_changed"] == 1
    assert [s["reason"] for s in res["skipped"]] == ["permission_denied"]
    assert res["undo_token"]
    assert a.read_text() == "y\n"
    assert b.read_text() == "x\n"
    fbs.undo_replace(res["undo_token"])
    assert a.read_text() == "x\n"


def test_replace_reports_undecodable_shown_file_as_skipped(tmp_path):
    good = _write(tmp_path, "good.txt", "hit\n")
    bad = tmp_path / "bad.txt"
    # ASCII 'hit' (so lossy search would surface it) but invalid UTF-8, so strict replace can't read it.
    bad.write_bytes(b"hit \xff\xfe nope\n")
    res = fbs.replace(str(tmp_path), "hit", "x", paths=[str(good), str(bad)])
    assert res["files_changed"] == 1
    assert [s["reason"] for s in res["skipped"]] == ["unreadable"]
    assert good.read_text() == "x\n"
    assert bad.read_bytes() == b"hit \xff\xfe nope\n"


def test_search_not_truncated_at_exact_match_cap(tmp_path):
    _write(tmp_path, "a.txt", "hit\n" * 5)
    res = fbs.search(str(tmp_path), "hit", max_matches=5)
    assert res["total_matches"] == 5
    assert res["truncated"] is False  # exactly the cap, no hidden 6th match


def test_search_not_truncated_at_exact_file_cap(tmp_path):
    for i in range(2):
        _write(tmp_path, f"f{i}.txt", "hit\n")
    res = fbs.search(str(tmp_path), "hit", max_files=2)
    assert res["total_files"] == 2
    assert res["truncated"] is False  # exactly the cap, no hidden 3rd file


def test_replace_truncates_on_file_cap(tmp_path):
    for i in range(4):
        _write(tmp_path, f"f{i}.txt", "hit\n")
    res = fbs.replace(str(tmp_path), "hit", "x", max_files=2)
    assert res["files_changed"] == 2
    assert res["truncated"] is True


def test_search_windows_preview_around_long_line_match(tmp_path):
    needle = "TARGET"
    line = "x" * 1000 + needle + "y" * 50
    _write(tmp_path, "long.txt", line + "\n")
    m = fbs.search(str(tmp_path), needle)["results"][0]["matches"][0]
    assert m["line_truncated"] is True
    # col/end are full-line offsets (the editor jump target), unaffected by the preview window.
    assert (m["col"], m["end"]) == (1000, 1006)
    # The windowed preview still contains the hit at its preview offsets.
    assert m["text"][m["preview_col"] : m["preview_end"]] == needle
    assert len(m["text"]) <= fbs.SEARCH_LINE_PREVIEW_CHARS + 1  # +1 for the leading ellipsis


def test_replace_bad_regex_template_is_clean_error(tmp_path):
    a = _write(tmp_path, "a.txt", "abc\n")
    # An invalid replacement template (unknown named group / out-of-range backref) must surface a
    # clean invalid_regex error, not leak IndexError/re.error as a 500.
    for bad in (r"\g<nope>", r"\9"):
        with pytest.raises(FileBrowserError) as exc:
            fbs.replace(str(tmp_path), "(a)", bad, regex=True, paths=[str(a)])
        assert exc.value.code == "invalid_regex"
    assert a.read_text() == "abc\n"


def test_replace_skips_vanished_explicit_path(tmp_path):
    a = _write(tmp_path, "a.txt", "hit\n")
    gone = str(tmp_path / "gone.txt")  # never created
    res = fbs.replace(str(tmp_path), "hit", "x", paths=[str(a), gone])
    assert res["files_changed"] == 1
    assert any(s["reason"] == "not_found" for s in res["skipped"])
    assert a.read_text() == "x\n"


def test_replace_skips_files_modified_since_search(tmp_path):
    a = _write(tmp_path, "a.txt", "hit\n")
    # A search-time mtime that no longer matches the file → treated as changed-since, left untouched.
    res = fbs.replace(str(tmp_path), "hit", "x", paths=[str(a)], expected_mtimes={str(a): 1.0})
    assert res["files_changed"] == 0
    assert [s["reason"] for s in res["skipped"]] == ["modified"]
    assert a.read_text() == "hit\n"


def test_replace_applies_when_search_mtime_matches(tmp_path):
    a = _write(tmp_path, "a.txt", "hit\n")
    mt = fbs.search(str(tmp_path), "hit")["results"][0]["mtime"]
    assert mt is not None
    res = fbs.replace(str(tmp_path), "hit", "x", paths=[str(a)], expected_mtimes={str(a): mt})
    assert res["files_changed"] == 1
    assert a.read_text() == "x\n"


def test_replace_skips_symlinked_explicit_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    real = root / "a.txt"
    real.write_text("hit\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("hit\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    res = fbs.replace(str(root), "hit", "x", paths=[str(real), str(root / "link.txt")])
    assert res["files_changed"] == 1
    assert any(s["reason"] == "symlink" for s in res["skipped"])
    assert real.read_text() == "x\n"
    assert outside.read_text() == "hit\n"  # symlink target never followed/written


def test_replace_empty_paths_is_noop(tmp_path):
    a = _write(tmp_path, "a.txt", "hit\n")
    res = fbs.replace(str(tmp_path), "hit", "x", paths=[])
    # An explicit empty selection must replace nothing — never fall through to a whole-tree walk.
    assert res["files_changed"] == 0
    assert res["undo_token"] is None
    assert a.read_text() == "hit\n"


def test_search_and_replace_handle_crlf_line_end(tmp_path):
    p = tmp_path / "win.txt"
    p.write_bytes(b"foo\r\nbar\r\n")
    # A line-end anchor matches despite the CRLF terminator...
    assert fbs.search(str(tmp_path), "foo$", regex=True)["total_matches"] == 1
    # ...and replace preserves the CRLF endings.
    fbs.replace(str(tmp_path), "foo$", "FOO", regex=True, paths=[str(p)])
    assert p.read_bytes() == b"FOO\r\nbar\r\n"


def test_undo_store_bounded_by_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(fbs, "_UNDO_MAX_BYTES", 10)
    a = _write(tmp_path, "a.txt", "hit hit\n")
    r1 = fbs.replace(str(tmp_path), "hit", "x", paths=[str(a)])
    b = _write(tmp_path, "b.txt", "hit\n")
    r2 = fbs.replace(str(tmp_path), "hit", "y", paths=[str(b)])
    # Storing r2 pushes total snapshot bytes over the budget, so r1's snapshot is evicted.
    with pytest.raises(FileBrowserError) as exc:
        fbs.undo_replace(r1["undo_token"])
    assert exc.value.code == "undo_unavailable"
    assert fbs.undo_replace(r2["undo_token"])["restored"] == [str(b)]


def test_search_root_must_be_directory(tmp_path):
    f = _write(tmp_path, "a.txt", "x\n")
    with pytest.raises(FileBrowserError):
        fbs.search(str(f), "x")


# ---------------------------------------------------------------------------
# Recursive name search (search_names): matches file AND folder names by name,
# recursively, unlike the content search above.
# ---------------------------------------------------------------------------


def _names(result: dict) -> set[str]:
    return {entry["rel"] for entry in result["results"]}


def test_search_names_matches_files_and_dirs_recursively(tmp_path):
    _write(tmp_path, "report.txt", "x")
    _write(tmp_path, "sub/report-2.md", "x")
    (tmp_path / "reports").mkdir()  # a DIRECTORY whose name matches — content search would miss this
    _write(tmp_path, "sub/deep/nope.txt", "x")

    result = fbs.search_names(str(tmp_path), "report")

    assert _names(result) == {"report.txt", "sub/report-2.md", "reports"}
    kinds = {entry["rel"]: entry["kind"] for entry in result["results"]}
    assert kinds["reports"] == "dir"
    assert kinds["report.txt"] == "file"
    assert result["truncated"] is False


def test_search_names_case_insensitive_substring(tmp_path):
    _write(tmp_path, "ReadMe.md", "x")
    _write(tmp_path, "notes/read_later.txt", "x")
    assert _names(fbs.search_names(str(tmp_path), "read")) == {"ReadMe.md", "notes/read_later.txt"}


def test_search_names_prunes_noise_dirs(tmp_path):
    # A match buried in node_modules must not surface, and the walk must not descend into it.
    _write(tmp_path, "node_modules/pkg/target.js", "x")
    _write(tmp_path, "src/target.ts", "x")
    assert _names(fbs.search_names(str(tmp_path), "target")) == {"src/target.ts"}


def test_search_names_honors_show_hidden(tmp_path):
    _write(tmp_path, ".secret-target", "x")
    _write(tmp_path, ".hidden/target.txt", "x")
    _write(tmp_path, "target.txt", "x")

    visible = fbs.search_names(str(tmp_path), "target")
    assert _names(visible) == {"target.txt"}

    # show_hidden descends into .hidden/ and matches dotfiles; the .hidden dir itself doesn't match
    # "target", so it isn't a result — but its matching child now is.
    hidden = fbs.search_names(str(tmp_path), "target", show_hidden=True)
    assert _names(hidden) == {".secret-target", ".hidden/target.txt", "target.txt"}


def test_search_names_empty_query_rejected(tmp_path):
    _write(tmp_path, "a.txt", "x")
    with pytest.raises(FileBrowserError) as exc:
        fbs.search_names(str(tmp_path), "   ")
    assert exc.value.code == "invalid_query"


def test_search_names_truncates_at_cap(tmp_path):
    for i in range(6):
        _write(tmp_path, f"match-{i}.txt", "x")
    result = fbs.search_names(str(tmp_path), "match", max_results=3)
    assert len(result["results"]) == 3
    assert result["truncated"] is True
    assert result["limit"] == 3


def test_search_names_exactly_at_cap_not_truncated(tmp_path):
    # Exactly max_results matches: everything is returned, so truncated must stay False (matches the
    # content search's before-record cap check; the naive after-append check would false-positive).
    for i in range(3):
        _write(tmp_path, f"match-{i}.txt", "x")
    result = fbs.search_names(str(tmp_path), "match", max_results=3)
    assert len(result["results"]) == 3
    assert result["truncated"] is False
    assert result["limit"] is None


def test_search_names_root_must_be_directory(tmp_path):
    f = _write(tmp_path, "a.txt", "x")
    with pytest.raises(FileBrowserError):
        fbs.search_names(str(f), "a")
