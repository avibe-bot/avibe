"""C-7 ``write`` and ``edit``: Pi's matching tiers and errors, ``replaceAll``, and all-or-nothing writes."""

from __future__ import annotations

import pytest

from core.agent_core.tools.edit import EditTool
from core.agent_core.tools.read import ReadTool
from core.agent_core.tools.write import WriteTool
from tests.agent_core.tools.conftest import result_text


async def test_write_creates_parent_directories_and_keeps_bytes(tmp_path, make_ctx):
    result = await WriteTool().execute({"path": "a/b/notes.txt", "content": "第一行\r\nsecond\n"}, make_ctx())

    assert not result.is_error
    assert result_text(result) == "Successfully wrote to a/b/notes.txt"
    assert (tmp_path / "a/b/notes.txt").read_bytes() == "第一行\r\nsecond\n".encode()


async def _edit(make_ctx, path, *edits, **extra):
    return await EditTool().execute({"path": path, "edits": list(edits), **extra}, make_ctx())


async def test_disjoint_edits_apply_against_the_original(tmp_path, make_ctx):
    (tmp_path / "f.py").write_text("a = 1\nb = 2\nc = 3\n")

    result = await _edit(
        make_ctx, "f.py", {"oldText": "a = 1", "newText": "a = 10"}, {"oldText": "c = 3", "newText": "c = 30"}
    )

    assert result_text(result) == "Successfully replaced 2 block(s) in f.py."
    assert (tmp_path / "f.py").read_text() == "a = 10\nb = 2\nc = 30\n"
    assert "+1 a = 10" in result.details["diff"]


async def test_normalized_match_rewrites_only_the_touched_lines(tmp_path, make_ctx):
    original = "title = “Hello”   \nkeep = ‘as is’  \nlast = 1\n"
    (tmp_path / "f.txt").write_text(original)

    # ASCII quotes and no trailing spaces: only Pi's normalized tier finds it.
    result = await _edit(make_ctx, "f.txt", {"oldText": 'title = "Hello"', "newText": 'title = "Hi"'})

    assert not result.is_error, result_text(result)
    assert (tmp_path / "f.txt").read_text() == 'title = "Hi"\nkeep = ‘as is’  \nlast = 1\n'


async def test_replace_all_replaces_every_occurrence(tmp_path, make_ctx):
    (tmp_path / "f.txt").write_text("foo(1)\nbar\nfoo(2)\nfoo(3)\n")

    result = await _edit(
        make_ctx,
        "f.txt",
        {"oldText": "foo(", "newText": "baz(", "replaceAll": True},
        {"oldText": "bar", "newText": "qux"},
    )

    assert result_text(result) == "Successfully replaced 2 block(s) in f.txt."
    assert (tmp_path / "f.txt").read_text() == "baz(1)\nqux\nbaz(2)\nbaz(3)\n"


@pytest.mark.parametrize("ending", ["\r\n", "\r"])
async def test_bom_and_line_endings_are_preserved(tmp_path, make_ctx, ending):
    (tmp_path / "f.txt").write_bytes(f"\ufeffone{ending}two{ending}three{ending}".encode())

    result = await _edit(make_ctx, "f.txt", {"oldText": "two\nthree", "newText": "2\n3"})

    assert not result.is_error, result_text(result)
    assert (tmp_path / "f.txt").read_bytes() == f"\ufeffone{ending}2{ending}3{ending}".encode()


async def test_an_exact_match_is_unique_even_with_a_normalized_twin(tmp_path, make_ctx):
    (tmp_path / "f.txt").write_text('say "Hi"\nsay \u201cHi\u201d\n')

    result = await _edit(make_ctx, "f.txt", {"oldText": 'say "Hi"', "newText": 'say "Hello"'})

    assert not result.is_error, result_text(result)
    assert (tmp_path / "f.txt").read_text() == 'say "Hello"\nsay \u201cHi\u201d\n'


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        (
            [{"oldText": "missing", "newText": "x"}],
            "Could not find the exact text in f.txt. The old text must match exactly including all whitespace and "
            "newlines.",
        ),
        (
            # Absent whitespace normalizes to nothing, which must not match everywhere.
            [{"oldText": "\t", "newText": "x"}],
            "Could not find the exact text in f.txt. The old text must match exactly including all whitespace and "
            "newlines.",
        ),
        (
            [{"oldText": "alpha", "newText": "A"}, {"oldText": "missing", "newText": "x"}],
            "Could not find edits[1] in f.txt. The oldText must match exactly including all whitespace and newlines.",
        ),
        (
            [{"oldText": "dup", "newText": "x"}],
            "Found 2 occurrences of the text in f.txt. The text must be unique. Please provide more context to make "
            "it unique.",
        ),
        (
            [{"oldText": "alpha", "newText": "A"}, {"oldText": "dup", "newText": "x"}],
            "Found 2 occurrences of edits[1] in f.txt. Each oldText must be unique. Please provide more context to "
            "make it unique.",
        ),
        (
            [{"oldText": "alpha beta", "newText": "x"}, {"oldText": "beta", "newText": "y"}],
            "edits[0] and edits[1] overlap in f.txt. Merge them into one edit or target disjoint regions.",
        ),
        (
            # An occurrence matched by replaceAll counts as a span for the overlap check.
            [{"oldText": "dup", "newText": "x", "replaceAll": True}, {"oldText": "dup two", "newText": "y"}],
            "edits[0] and edits[1] overlap in f.txt. Merge them into one edit or target disjoint regions.",
        ),
        (
            [{"oldText": "alpha", "newText": "A"}, {"oldText": "", "newText": "x"}],
            "edits[1].oldText must not be empty in f.txt.",
        ),
    ],
)
async def test_a_failing_edit_writes_nothing(tmp_path, make_ctx, edits, message):
    original = "alpha beta\ndup one\ndup two\n"
    (tmp_path / "f.txt").write_text(original)

    result = await _edit(make_ctx, "f.txt", *edits)

    assert result.is_error
    assert result_text(result) == message
    assert (tmp_path / "f.txt").read_text() == original


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ReadTool(), {"path": "a\x00b"}),
        (WriteTool(), {"path": "a\x00b", "content": "x"}),
        (EditTool(), {"path": "file:///tmp/a%00b", "edits": [{"oldText": "a", "newText": "b"}]}),
    ],
)
async def test_a_path_no_file_can_have_is_an_error_result(make_ctx, tool, arguments):
    result = await tool.execute(arguments, make_ctx())

    assert result.is_error
    assert result_text(result) == f"Invalid path: {arguments['path']!r} contains a NUL byte"
