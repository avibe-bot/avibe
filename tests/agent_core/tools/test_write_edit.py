"""C-7 ``write`` and ``edit``: Pi's matching tiers and errors, ``replaceAll``, and all-or-nothing writes."""

from __future__ import annotations

import errno
import random
import re
import stat

import pytest

import core.agent_core.tools.paths as paths_module
import core.agent_core.tools.write as write_module

import core.agent_core.tools.edit as edit_module
from core.agent_core.tools.args import ToolInputError
from core.agent_core.tools.edit import EditTool
from core.agent_core.tools.read import ReadTool
from core.agent_core.tools.write import WriteTool
from tests.agent_core.tools.conftest import result_text


async def test_write_creates_parent_directories_and_keeps_bytes(tmp_path, make_ctx):
    result = await WriteTool().execute({"path": "a/b/notes.txt", "content": "第一行\r\nsecond\n"}, make_ctx())

    assert not result.is_error
    assert result_text(result) == "Successfully wrote to a/b/notes.txt"
    assert (tmp_path / "a/b/notes.txt").read_bytes() == "第一行\r\nsecond\n".encode()


async def test_a_failed_edit_write_leaves_the_original_intact(tmp_path, make_ctx, monkeypatch):
    (tmp_path / "f.txt").write_text("original\n")

    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(write_module.os, "fsync", disk_full)
    result = await _edit(make_ctx, "f.txt", {"oldText": "original", "newText": "changed"})

    assert (result.is_error, result_text(result)) == (True, "Could not edit file: f.txt. Error code: ENOSPC.")
    assert (tmp_path / "f.txt").read_text() == "original\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]


@pytest.mark.parametrize("existing", [True, False])
async def test_a_failed_write_leaves_the_original_intact(tmp_path, make_ctx, monkeypatch, existing):
    if existing:
        (tmp_path / "f.txt").write_text("original\n")

    def disk_full(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(write_module.os, "fsync", disk_full)
    result = await WriteTool().execute({"path": "f.txt", "content": "replacement\n"}, make_ctx())

    assert result.is_error
    if existing:
        assert (tmp_path / "f.txt").read_text() == "original\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == (["f.txt"] if existing else [])


async def test_write_goes_through_a_symlink_and_keeps_the_mode(tmp_path, make_ctx):
    target = tmp_path / "real.sh"
    target.write_text("old\n")
    target.chmod(0o751)
    (tmp_path / "link.sh").symlink_to(target)

    result = await WriteTool().execute({"path": "link.sh", "content": "new\n"}, make_ctx())

    assert not result.is_error, result_text(result)
    assert (tmp_path / "link.sh").is_symlink()
    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751


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


async def test_text_copied_from_read_edits_a_file_with_invalid_bytes(tmp_path, make_ctx):
    """read and edit show undecodable bytes the same way, so a block copied from read matches."""
    (tmp_path / "f.txt").write_bytes(b"a\xffb\xe2\x82c\nkeep\x80\n")
    shown = result_text(await ReadTool().execute({"path": "f.txt"}, make_ctx()))
    first_line = shown.split("\n")[0]

    result = await _edit(make_ctx, "f.txt", {"oldText": first_line, "newText": "fixed"})

    assert not result.is_error, result_text(result)
    assert (tmp_path / "f.txt").read_bytes() == b"fixed\nkeep\x80\n"


async def test_text_copied_from_read_edits_the_first_line_of_a_bom_file(tmp_path, make_ctx):
    """read does not show the BOM, edit does not match it, and the BOM survives."""
    (tmp_path / "f.txt").write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\n")
    shown = result_text(await ReadTool().execute({"path": "f.txt"}, make_ctx()))
    first_line = shown.split("\n")[0]

    result = await _edit(make_ctx, "f.txt", {"oldText": first_line, "newText": "ALPHA"})

    assert shown == "alpha\nbeta\n"
    assert not result.is_error, result_text(result)
    assert (tmp_path / "f.txt").read_bytes() == b"\xef\xbb\xbfALPHA\r\nbeta\r\n"


async def test_a_planned_result_over_the_limit_is_refused_and_its_diff_skipped(tmp_path, make_ctx, monkeypatch):
    (tmp_path / "small.txt").write_text("seed\n")
    monkeypatch.setattr(edit_module, "MAX_EDIT_BYTES", 1000)
    monkeypatch.setattr(edit_module, "MAX_DIFF_BYTES", 400)

    grown = await _edit(make_ctx, "small.txt", {"oldText": "seed", "newText": "x" * 600})
    too_big = await _edit(make_ctx, "small.txt", {"oldText": "x" * 600, "newText": "y" * 2000})

    assert grown.details == {"diff_skipped": True}
    assert too_big.is_error
    assert result_text(too_big) == (
        "File small.txt would be 2.0KB after this edit, over the 1000B edit limit. "
        "Use bash (for example sed or a short script) to change files this large."
    )
    assert (tmp_path / "small.txt").read_text() == "x" * 600 + "\n"


async def test_files_over_the_edit_limit_are_refused_and_large_diffs_skipped(tmp_path, make_ctx, monkeypatch):
    (tmp_path / "big.log").write_text("line\n" * 100)
    monkeypatch.setattr(edit_module, "MAX_EDIT_BYTES", 400)

    refused = await _edit(make_ctx, "big.log", {"oldText": "line", "newText": "x", "replaceAll": True})

    assert refused.is_error
    assert result_text(refused) == (
        "File big.log is 500B, over the 400B edit limit. "
        "Use bash (for example sed or a short script) to change files this large."
    )
    assert (tmp_path / "big.log").read_text() == "line\n" * 100

    monkeypatch.setattr(edit_module, "MAX_EDIT_BYTES", 10_000)
    monkeypatch.setattr(edit_module, "MAX_DIFF_BYTES", 400)
    edited = await _edit(make_ctx, "big.log", {"oldText": "line", "newText": "x", "replaceAll": True})

    assert not edited.is_error, result_text(edited)
    assert edited.details == {"diff_skipped": True}


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


L, R = "\u201c", "\u201d"  # smart double quotes


@pytest.mark.parametrize(
    ("original", "edits", "expected"),
    [
        # An exact replaceAll beside a normalized edit replaces only exact occurrences.
        (
            f'say "A"\nsay {L}A{R}\ntitle = {L}T{R}   \n',
            [
                {"oldText": 'say "A"', "newText": 'say "B"', "replaceAll": True},
                {"oldText": 'title = "T"', "newText": 'title = "U"'},
            ],
            f'say "B"\nsay {L}A{R}\ntitle = "U"\n',
        ),
        # An exact edit stays unique beside a normalized edit, though their normalized forms collide.
        (
            f'x = "1"\nx = {L}1{R}\ny = \u20182\u2019  \n',
            [{"oldText": 'x = "1"', "newText": 'x = "9"'}, {"oldText": "y = '2'", "newText": "y = '3'"}],
            f"x = \"9\"\nx = {L}1{R}\ny = '3'\n",
        ),
        # replaceAll uses the first tier with a match: exact occurrences only.
        (
            f'a = {L}q{R}\nc = "q"\n',
            [{"oldText": '= "q"', "newText": '= "r"', "replaceAll": True}],
            f'a = {L}q{R}\nc = "r"\n',
        ),
        # With no exact occurrence, every normalized occurrence is rewritten, line by line.
        (
            f"a = {L}q{R}  \nkeep \u2014 me  \nb = {L}q{R}\n",
            [{"oldText": '= "q"', "newText": '= "r"', "replaceAll": True}],
            'a = "r"\nkeep \u2014 me  \nb = "r"\n',
        ),
        # Two normalized edits on one line apply together.
        (
            f"k = {L}v{R}; j = {L}w{R}\n",
            [{"oldText": 'k = "v"', "newText": 'k = "1"'}, {"oldText": 'j = "w"', "newText": 'j = "2"'}],
            'k = "1"; j = "2"\n',
        ),
    ],
)
async def test_each_edit_matches_in_its_own_tier(tmp_path, make_ctx, original, edits, expected):
    (tmp_path / "f.txt").write_text(original)

    result = await _edit(make_ctx, "f.txt", *edits)

    assert not result.is_error, result_text(result)
    assert (tmp_path / "f.txt").read_text() == expected


_BREAKS = ["\r\n", "\r", "\n"]
_WORDS = ["alpha", "beta", "gamma", "x = 1", "  ", "\u201cq\u201d", "\u00e9"]


def _random_file(rng: random.Random) -> list[tuple[str, str]]:
    """Lines with their own breaks, as the file reads back: ``"\\r"`` then ``"\\n"`` is one CRLF."""
    body = (
        "".join(" ".join(rng.choice(_WORDS) for _ in range(rng.randint(0, 3))) + rng.choice(_BREAKS) for _ in range(7))
        + " ".join(rng.choice(_WORDS) for _ in range(rng.randint(0, 3)))
        + rng.choice(["", *_BREAKS])
    )
    parts = re.split(r"(\r\n|\r|\n)", body)
    return list(zip(parts[0::2], [*parts[1::2], ""]))


async def test_an_edit_never_changes_bytes_outside_the_lines_it_replaces(tmp_path, make_ctx):
    """Mixed line breaks, a BOM, and an invalid byte survive every edit that does not cover them."""
    rng = random.Random(11)
    path = tmp_path / "f.txt"
    for _ in range(300):
        lines = _random_file(rng)
        view = "\n".join(content for content, _ in lines) + ("\n" if lines[-1][1] else "")
        first, last = sorted(rng.sample(range(len(lines)), 2))
        old = "\n".join(content for content, _ in lines[first : last + 1])
        if not old.strip() or view.count(old) != 1:
            continue
        prefix = "".join(c + b for c, b in lines[:first])
        suffix = lines[last][1] + "".join(c + b for c, b in lines[last + 1 :])
        # The replaced lines in the file keep their own breaks; oldText uses LF, as the model sends it.
        middle = "".join(c + b for c, b in lines[first:last]) + lines[last][0]
        raw = b"\xef\xbb\xbf\xff" + (prefix + middle + suffix).encode()
        path.write_bytes(raw)

        result = await _edit(make_ctx, "f.txt", {"oldText": old, "newText": "NEW"})

        assert not result.is_error, (result_text(result), lines, old)
        assert path.read_bytes() == b"\xef\xbb\xbf\xff" + (prefix + "NEW" + suffix).encode(), (lines, old)


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
            # A normalized edit rewrites its whole line, so an exact edit on that line overlaps it.
            [{"oldText": "alpha", "newText": "A"}, {"oldText": "beta  ", "newText": "B"}],
            "edits[0] and edits[1] overlap in f.txt. Merge them into one edit or target disjoint regions.",
        ),
        (
            [],
            "Edit tool input is invalid. edits must contain at least one replacement.",
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


@pytest.mark.parametrize(
    ("windows", "url", "expected"),
    [
        (False, "file:///tmp/a%20b.txt", "/tmp/a b.txt"),
        (False, "file://localhost/tmp/x", "/tmp/x"),
        (False, "file://server/tmp/x", ToolInputError("Invalid path: file URL host must be empty or localhost")),
        (False, "file:///tmp/a%2Fb", ToolInputError("Invalid path: a file URL must not include encoded / characters")),
        (False, "file:///tmp/%FF", ToolInputError("Invalid path: a file URL must use valid percent-encoded UTF-8")),
        (False, "file:///tmp/%ZZ", ToolInputError("Invalid path: a file URL must use valid percent-encoded UTF-8")),
        (True, "file:///C:/%E2%82", ToolInputError("Invalid path: a file URL must use valid percent-encoded UTF-8")),
        (True, "file:///C:/tmp/a%20b.txt", "C:\\tmp\\a b.txt"),
        (True, "file://localhost/C:/x", "C:\\x"),
        (True, "file://server/share/x.txt", "\\\\server\\share\\x.txt"),
        (True, "file:///tmp/x", ToolInputError("Invalid path: a file URL must be absolute")),
        (
            True,
            "file:///C:/a%5Cb",
            ToolInputError("Invalid path: a file URL must not include encoded \\ or / characters"),
        ),
    ],
)
def test_file_urls_follow_node_file_url_to_path(monkeypatch, windows, url, expected):
    monkeypatch.setattr(paths_module, "_WINDOWS", windows)

    if isinstance(expected, ToolInputError):
        with pytest.raises(ToolInputError) as raised:
            paths_module.expand_path(url)
        assert str(raised.value) == str(expected)
    else:
        assert paths_module.expand_path(url) == expected
