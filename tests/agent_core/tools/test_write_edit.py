"""C-7 ``write`` and ``edit``: Pi's matching tiers and errors, ``replaceAll``, and all-or-nothing writes."""

from __future__ import annotations

import asyncio
import errno
import os
import random
import re
import stat
import threading
import time
import tracemalloc

import pytest

import core.agent_core.tools.paths as paths_module
import core.agent_core.tools.write as write_module

import core.agent_core.tools.edit as edit_module
import core.agent_core.tools.edit_diff as edit_diff_module
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


@pytest.mark.parametrize("data", [b"alpha\r\r\nbeta\nkeep\n", b"alpha\r\nbeta\nkeep\n", b"a\rlpha\r\r\r\nbeta\nkeep\n"])
async def test_lines_copied_from_read_edit_the_file_whatever_their_carriage_returns(tmp_path, make_ctx, data):
    """read's view is edit's matching view: two lines copied from read are found, ``\\r\\r\\n`` included."""
    (tmp_path / "f.txt").write_bytes(data)
    shown = result_text(await ReadTool().execute({"path": "f.txt"}, make_ctx()))
    first_two = shown.split("\nkeep")[0]

    result = await _edit(make_ctx, "f.txt", {"oldText": first_two, "newText": "fixed"})

    assert not result.is_error, result_text(result)
    assert (tmp_path / "f.txt").read_bytes() == b"fixed\nkeep\n"


async def test_text_copied_from_read_edits_the_first_line_of_a_bom_file(tmp_path, make_ctx):
    """read does not show the BOM, edit does not match it, and the BOM survives."""
    (tmp_path / "f.txt").write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\r\n")
    shown = result_text(await ReadTool().execute({"path": "f.txt"}, make_ctx()))
    first_line = shown.split("\n")[0]

    result = await _edit(make_ctx, "f.txt", {"oldText": first_line, "newText": "ALPHA"})

    assert shown == "alpha\nbeta\n"
    assert not result.is_error, result_text(result)
    assert (tmp_path / "f.txt").read_bytes() == b"\xef\xbb\xbfALPHA\r\nbeta\r\n"


async def test_edit_takes_the_size_from_the_file_it_reads(tmp_path, make_ctx, monkeypatch):
    """The file grew after a check by path: the limit holds on the descriptor actually read."""
    (tmp_path / "f.txt").write_text("hello\n" + "x" * 2000)
    monkeypatch.setattr(edit_module, "MAX_EDIT_BYTES", 1000)
    monkeypatch.setattr(edit_module.os.path, "getsize", lambda path: 10)  # what an earlier check by path saw

    result = await _edit(make_ctx, "f.txt", {"oldText": "hello", "newText": "bye"})

    assert result.is_error
    assert result_text(result).startswith("File f.txt is 2.0KB, over the 1000B edit limit.")
    assert (tmp_path / "f.txt").read_text().startswith("hello")


async def test_replacement_contents_stay_private_until_the_rename(tmp_path, make_ctx, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("old\n")
    secret.chmod(0o600)
    seen = []
    real_fsync = write_module.os.fsync

    def watch(fd):
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(write_module.os, "fsync", watch)
    result = await WriteTool().execute({"path": "token", "content": "new secret\n"}, make_ctx())

    assert not result.is_error, result_text(result)
    assert seen and all(mode & 0o077 == 0 for mode in seen)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert secret.read_text() == "new secret\n"


_WRITE_FIRST = {"path": "f.txt", "content": "first\n"}


@pytest.mark.parametrize(
    ("first", "cancels", "worker_fails"),
    [
        (_WRITE_FIRST, 1, False),
        ({"path": "f.txt", "edits": [{"oldText": "original", "newText": "first"}]}, 1, False),
        (_WRITE_FIRST, 2, False),
        (_WRITE_FIRST, 1, True),
    ],
    ids=["write", "edit", "cancelled-twice", "worker-fails"],
)
async def test_a_cancelled_write_lands_before_the_next_writer_takes_the_lock(
    tmp_path, make_ctx, monkeypatch, first, cancels, worker_fails
):
    """Cancelling an await does not stop its worker thread; the lock is held until the worker is done."""
    (tmp_path / "f.txt").write_text("original\n")
    entered, release, renamed = threading.Event(), threading.Event(), threading.Event()
    real_fsync, real_replace, calls, renames = write_module.os.fsync, write_module.os.replace, [], []
    expected_renames = 1 if worker_fails else 2

    def first_fsync_blocks(fd):
        calls.append(fd)
        if len(calls) == 1:
            entered.set()
            release.wait(10)
            if worker_fails:
                raise OSError(errno.ENOSPC, "No space left on device")
        real_fsync(fd)

    def counted_replace(source, target):
        real_replace(source, target)
        renames.append(target)
        if len(renames) == expected_renames:
            renamed.set()

    monkeypatch.setattr(write_module.os, "fsync", first_fsync_blocks)
    monkeypatch.setattr(write_module.os, "replace", counted_replace)
    tool = WriteTool() if "content" in first else EditTool()
    cancelled = asyncio.ensure_future(tool.execute(first, make_ctx()))
    await asyncio.to_thread(entered.wait, 10)
    for _ in range(cancels):
        cancelled.cancel()
        await asyncio.sleep(0.05)
    later = asyncio.ensure_future(WriteTool().execute({"path": "f.txt", "content": "second\n"}, make_ctx()))
    await asyncio.sleep(0.3)  # time for the later write to get through a lock released too early
    release.set()
    outcomes = await asyncio.gather(cancelled, later, return_exceptions=True)
    assert await asyncio.to_thread(renamed.wait, 10)

    # The cancel propagates even when the worker failed; it is never turned into a result.
    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert not outcomes[1].is_error, result_text(outcomes[1])
    assert (tmp_path / "f.txt").read_text() == "second\n"


@pytest.mark.parametrize("change", ["replaced", "retargeted", "rewritten"])
async def test_an_edit_publishes_only_over_the_file_it_read(tmp_path, make_ctx, monkeypatch, change):
    """Another writer (bash, an editor) changed the file after edit read it: nothing is written over it."""
    (tmp_path / "d").mkdir()
    real, other, link = tmp_path / "d" / "a.txt", tmp_path / "d" / "b.txt", tmp_path / "d" / "f.txt"
    real.write_text("alpha\n")
    other.write_text("other\n")
    link.symlink_to(real)
    real_plan = edit_module._plan_edits

    def plan_then_change(*args):
        planned = real_plan(*args)
        if change == "replaced":
            (tmp_path / "d" / "new").write_text("unrelated text\n")
            os.replace(tmp_path / "d" / "new", real)
        elif change == "retargeted":
            link.unlink()
            link.symlink_to(other)
        else:
            real.write_text("unrelated text\n")
        return planned

    monkeypatch.setattr(edit_module, "_plan_edits", plan_then_change)
    result = await _edit(make_ctx, "d/f.txt", {"oldText": "alpha", "newText": "beta"})

    assert (result.is_error, result_text(result)) == (
        True,
        "Could not edit file: d/f.txt. It changed while the edit was being applied; read it again.",
    )
    expected = {"retargeted": ("alpha\n", "other\n")}.get(change, ("unrelated text\n", "other\n"))
    assert (real.read_text(), other.read_text()) == expected


@pytest.mark.parametrize("swap", ["fifo", "read-only", "directory"])
async def test_write_revalidates_the_target_right_before_replacing_it(tmp_path, make_ctx, monkeypatch, swap):
    """Another process swapped the file after write classified it: nothing that is not a writable regular file
    is ever replaced."""
    (tmp_path / "f").write_text("old\n")
    real_prepare = write_module._prepare_target

    def prepare_then_swap(absolute):
        refusal = real_prepare(absolute)
        os.remove(absolute)
        if swap == "fifo":
            os.mkfifo(absolute)
        elif swap == "read-only":
            with open(absolute, "w") as handle:
                handle.write("theirs\n")
            os.chmod(absolute, 0o444)
        else:
            os.mkdir(absolute)
        return refusal

    monkeypatch.setattr(write_module, "_prepare_target", prepare_then_swap)
    result = await WriteTool().execute({"path": "f", "content": "new\n"}, make_ctx())

    reason = {"fifo": "it is not a regular file", "read-only": "permission denied", "directory": "it is a directory"}
    assert (result.is_error, result_text(result)) == (True, f"Cannot write f: {reason[swap]}.")
    kind = os.lstat(tmp_path / "f").st_mode
    assert {"fifo": stat.S_ISFIFO, "read-only": stat.S_ISREG, "directory": stat.S_ISDIR}[swap](kind)
    if swap == "read-only":
        assert (tmp_path / "f").read_text() == "theirs\n"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]


@pytest.mark.parametrize("tool", ["write", "edit"])
async def test_a_name_at_the_length_limit_can_be_replaced(tmp_path, make_ctx, tool):
    """The temp file beside the target must not be longer than the target's own (valid) name."""
    name = "n" * 251 + ".txt"  # 255 bytes, the usual NAME_MAX
    (tmp_path / name).write_text("old\n")

    if tool == "write":
        result = await WriteTool().execute({"path": name, "content": "new\n"}, make_ctx())
    else:
        result = await _edit(make_ctx, name, {"oldText": "old", "newText": "new"})

    assert not result.is_error, result_text(result)
    assert (tmp_path / name).read_text() == "new\n"


@pytest.mark.parametrize("tool", ["write", "edit"])
async def test_a_path_retargeted_during_the_write_is_not_written(tmp_path, make_ctx, monkeypatch, tool):
    """The path names another file by the time of the rename: the old target is not written behind its back."""
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    (tmp_path / "f.txt").symlink_to(tmp_path / "a.txt")
    real_fsync = write_module.os.fsync

    def fsync_then_retarget(fd):
        real_fsync(fd)
        (tmp_path / "f.txt").unlink()
        (tmp_path / "f.txt").symlink_to(tmp_path / "b.txt")

    monkeypatch.setattr(write_module.os, "fsync", fsync_then_retarget)
    if tool == "write":
        result = await WriteTool().execute({"path": "f.txt", "content": "new\n"}, make_ctx())
        expected = "Cannot write f.txt: it changed while it was being written; write it again."
    else:
        result = await _edit(make_ctx, "f.txt", {"oldText": "alpha", "newText": "new"})
        expected = "Could not edit file: f.txt. It changed while the edit was being applied; read it again."

    assert (result.is_error, result_text(result)) == (True, expected)
    assert ((tmp_path / "a.txt").read_text(), (tmp_path / "b.txt").read_text()) == ("alpha\n", "beta\n")


async def test_new_files_keep_the_umask_default(tmp_path, make_ctx):
    umask = os.umask(0)
    os.umask(umask)

    await WriteTool().execute({"path": "fresh.txt", "content": "x"}, make_ctx())

    assert stat.S_IMODE((tmp_path / "fresh.txt").stat().st_mode) == 0o666 & ~umask


async def test_write_and_edit_sanitize_model_text_the_same_way(tmp_path, make_ctx):
    await WriteTool().execute({"path": "w.txt", "content": "a\ud800b"}, make_ctx())
    (tmp_path / "e.txt").write_text("a-b")
    await _edit(make_ctx, "e.txt", {"oldText": "-", "newText": "\udfff"})

    assert (tmp_path / "w.txt").read_bytes() == "a\ufffdb".encode()
    assert (tmp_path / "e.txt").read_bytes() == "a\ufffdb".encode()


async def test_the_display_diff_is_bounded_by_lines_not_only_bytes(tmp_path, make_ctx):
    """Repetitive lines made difflib quadratic (Codex measured over 22 s); the changed middle is all it sees."""
    (tmp_path / "rep.txt").write_text("x\n" * 5000 + "target\n" + "x\n" * 5000)
    started = time.monotonic()

    result = await _edit(make_ctx, "rep.txt", {"oldText": "target", "newText": "changed"})

    assert time.monotonic() - started < 2.0
    assert not result.is_error, result_text(result)
    # Pi pads line numbers to the widest one (five digits here).
    assert "- 5001 target\n+ 5001 changed" in result.details["diff"]
    assert "@@ -4997,9 +4997,9 @@" in result.details["patch"]


# Expected values are the output of Pi's generateDiffString and generateUnifiedPatch (jsdiff 8.0.4).
@pytest.mark.parametrize(
    ("original", "edit", "diff", "first_changed_line", "patch"),
    [
        (
            "x\na",
            {"oldText": "a", "newText": "b"},
            " 1 x\n-2 a\n+2 b",
            2,
            "@@ -1,2 +1,2 @@\n x\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n",
        ),
        (
            "a\n",
            {"oldText": "a\n", "newText": "a"},
            "-1 a\n+1 a",
            1,
            "@@ -1,1 +1,1 @@\n-a\n+a\n\\ No newline at end of file\n",
        ),
        (
            # A form feed is not a line break.
            "a\fb\nc\n",
            {"oldText": "c", "newText": "C"},
            " 1 a\fb\n-2 c\n+2 C",
            2,
            "@@ -1,2 +1,2 @@\n a\fb\n-c\n+C\n",
        ),
        (
            "one\n",
            {"oldText": "one\n", "newText": "one\ntwo\n"},
            " 1 one\n+2 two",
            2,
            "@@ -1,1 +1,2 @@\n one\n+two\n",
        ),
        (
            "".join(f"{i}\n" for i in range(1, 9)) + "9",
            {"oldText": "9", "newText": "nine"},
            "   ...\n 5 5\n 6 6\n 7 7\n 8 8\n-9 9\n+9 nine",
            9,
            "@@ -5,5 +5,5 @@\n 5\n 6\n 7\n 8\n-9\n\\ No newline at end of file\n+nine\n\\ No newline at end of file\n",
        ),
    ],
    ids=["no-final-newline", "final-newline-removed", "form-feed", "insertion", "width"],
)
async def test_the_display_diff_and_patch_are_pis(tmp_path, make_ctx, original, edit, diff, first_changed_line, patch):
    (tmp_path / "f.txt").write_bytes(original.encode())

    result = await _edit(make_ctx, "f.txt", edit)

    assert not result.is_error, result_text(result)
    assert result.details == {
        "diff": diff,
        "patch": "--- f.txt\n+++ f.txt\n" + patch,
        "first_changed_line": first_changed_line,
    }


@pytest.mark.parametrize(
    ("content", "edit"),
    [
        # Normalized matching strips trailing whitespace per line; a long run must not be quadratic.
        ("x" + " " * 60_000 + "y\n", {"oldText": "\u201cmissing\u201d", "newText": "z"}),
        # replaceAll over one long line: tens of thousands of occurrences.
        ("a," * 40_000 + "\n", {"oldText": "a", "newText": "b", "replaceAll": True}),
    ],
    ids=["whitespace-run", "replace-all"],
)
async def test_edit_work_stays_linear_on_adversarial_files(tmp_path, make_ctx, content, edit):
    (tmp_path / "f.txt").write_text(content)
    started = time.monotonic()

    await _edit(make_ctx, "f.txt", edit)

    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    ("content", "edit", "is_error"),
    [
        # A unique edit stops looking after the second match; the count comes from str.count.
        ("x" * 1_000_000, {"oldText": "x", "newText": "y"}, True),
        # More lines than edit plans per-line state for: refused before any is built.
        ("\n" * 1_000_000 + "t", {"oldText": "t", "newText": "u"}, True),
        # More occurrences than replaceAll builds replacements for: refused before any is built.
        ("x" * 1_000_000, {"oldText": "x", "newText": "y", "replaceAll": True}, True),
        # A result of three million lines, within the byte limit: its view is built without per-line state.
        ("t," * 10, {"oldText": "t", "newText": "\n" * 300_000, "replaceAll": True}, False),
        # oldText with more lines than the file cannot match, and is never normalized line by line.
        ("zz\n", {"oldText": "ab\n" * 1_000_000, "newText": "y"}, True),
    ],
    ids=["duplicate", "many-lines", "many-occurrences", "many-result-lines", "long-old-text"],
)
async def test_edit_memory_is_bounded_by_its_budgets_not_by_the_file(tmp_path, make_ctx, content, edit, is_error):
    """Python objects per line or per match cost hundreds of bytes each, in the process every Session shares."""
    (tmp_path / "f.txt").write_text(content)
    tracemalloc.start()
    try:
        result = await _edit(make_ctx, "f.txt", edit)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert result.is_error == is_error, result_text(result)
    assert peak < 32 * 1024 * 1024


async def test_edit_refuses_files_with_more_lines_than_it_plans(tmp_path, make_ctx, monkeypatch):
    monkeypatch.setattr(edit_module, "MAX_EDIT_LINES", 100)
    (tmp_path / "ok.txt").write_text("x\n" * 99 + "t")
    (tmp_path / "big.txt").write_text("x\r\n" * 100 + "t")

    ok = await _edit(make_ctx, "ok.txt", {"oldText": "t", "newText": "u"})
    big = await _edit(make_ctx, "big.txt", {"oldText": "t", "newText": "u"})

    assert not ok.is_error, result_text(ok)
    assert (big.is_error, result_text(big)) == (
        True,
        "File big.txt has 101 lines, over the 100 line edit limit. "
        "Use bash (for example sed or a short script) to change files this large.",
    )


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        (
            [{"oldText": "a", "newText": "b", "replaceAll": True}],
            "Found 11 occurrences of the text in f.txt, over the 10 replaceAll limit. "
            "Use bash (for example sed or a short script) to replace this many.",
        ),
        (
            # The budget is for the whole call: 11 + 10 replacements by two edits, each under it alone.
            [
                {"oldText": "a", "newText": "b", "replaceAll": True},
                {"oldText": "c", "newText": "d", "replaceAll": True},
            ],
            "The edits make 21 replacements in f.txt, over the 19 replacement limit. "
            "Use bash (for example sed or a short script) to replace this many.",
        ),
        (
            # Ten replacements of 200 characters: over a 1,000-byte limit before anything is built.
            [{"oldText": "c", "newText": "y" * 200, "replaceAll": True}],
            "File f.txt would be over the 1000B edit limit after this edit. "
            "Use bash (for example sed or a short script) to change files this large.",
        ),
    ],
    ids=["single", "batch-total", "inserted-text"],
)
async def test_replace_all_has_a_budget(tmp_path, make_ctx, monkeypatch, edits, message):
    monkeypatch.setattr(edit_module, "MAX_EDIT_BYTES", 1000)
    monkeypatch.setattr(edit_diff_module, "MAX_REPLACEMENTS", 10 if len(edits) == 1 else 19)
    (tmp_path / "f.txt").write_text("a," * 11 + "zz\n" + "c," * 10 + "\n")

    result = await _edit(make_ctx, "f.txt", *edits)

    assert (result.is_error, result_text(result)) == (True, message)
    assert (tmp_path / "f.txt").read_text() == "a," * 11 + "zz\n" + "c," * 10 + "\n"


async def test_edit_work_has_a_budget_over_all_edits(tmp_path, make_ctx, monkeypatch):
    """Each edit scans the whole file, holding the GIL every Session shares: edits × size is bounded."""
    monkeypatch.setattr(edit_diff_module, "MAX_EDIT_SCAN_CHARS", 1000)
    (tmp_path / "f.txt").write_text("".join(f"line {i}\n" for i in range(100)))  # 790 characters
    edits = [{"oldText": f"line {i}\n", "newText": f"LINE {i}\n"} for i in range(2)]

    one = await _edit(make_ctx, "f.txt", edits[0])
    two = await _edit(make_ctx, "f.txt", *edits)

    assert not one.is_error, result_text(one)
    assert (two.is_error, result_text(two)) == (
        True,
        "2 edits over f.txt (790 characters) are over the edit work limit. Split them into several edit calls, "
        "or use bash (for example sed or a short script).",
    )


async def test_the_display_diff_never_splits_a_result_with_too_many_lines(tmp_path, make_ctx):
    """A result under the diff byte gate can still have a million lines; their count is checked first."""
    (tmp_path / "f.txt").write_text("t\n" + "x\n" * 100)
    tracemalloc.start()
    try:
        result = await _edit(make_ctx, "f.txt", {"oldText": "t", "newText": "ab\n" * 300_000})
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert result.details == {"diff_skipped": True}
    assert peak < 8 * 1024 * 1024


async def test_a_large_changed_middle_skips_the_display_diff(tmp_path, make_ctx):
    (tmp_path / "f.txt").write_text("".join(f"line {i}\n" for i in range(3000)))

    result = await _edit(
        make_ctx,
        "f.txt",
        {"oldText": "line 0\n", "newText": "first\n"},
        {"oldText": "line 2999\n", "newText": "last\n"},
    )

    assert not result.is_error, result_text(result)
    assert result.details == {"diff_skipped": True}


async def test_a_planned_result_over_the_limit_is_refused_and_its_diff_skipped(tmp_path, make_ctx, monkeypatch):
    (tmp_path / "small.txt").write_text("seed\n")
    monkeypatch.setattr(edit_module, "MAX_EDIT_BYTES", 1000)
    monkeypatch.setattr(edit_module, "MAX_DIFF_BYTES", 400)

    grown = await _edit(make_ctx, "small.txt", {"oldText": "seed", "newText": "x" * 600})
    too_big = await _edit(make_ctx, "small.txt", {"oldText": "x" * 600, "newText": "y" * 2000})

    assert grown.details == {"diff_skipped": True}
    assert too_big.is_error
    assert result_text(too_big) == (
        "File small.txt would be over the 1000B edit limit after this edit. "
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
    ("tool", "arguments", "expected"),
    [
        (ReadTool(), {"path": "a\ud800"}, "Cannot read a\ufffd: no such file or directory."),
        (WriteTool(), {"path": "a\udfff", "content": "x"}, "Successfully wrote to a\ufffd"),
    ],
)
async def test_lone_surrogates_in_a_path_are_sanitized(tmp_path, make_ctx, tool, arguments, expected):
    result = await tool.execute(arguments, make_ctx())

    assert result_text(result) == expected


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
    "url",
    [
        "file://h%C3%A9/x",  # Node: \\\\hé\\x, through IDNA processing Python does not have
        "file://xn--9ca/x",  # the same host, spelled in punycode
        "file://127.1/x",  # Node: \\\\127.0.0.1\\x, after WHATWG's IPv4 parsing
        "file://[::ffff:1.2.3.4]/x",  # Node: \\\\[::ffff:102:304]\\x
    ],
)
def test_unc_hosts_that_need_idna_or_address_parsing_are_refused(monkeypatch, url):
    """Refused rather than guessed, so a URL never names a share Node would not."""
    monkeypatch.setattr(paths_module, "_WINDOWS", True)

    with pytest.raises(ToolInputError) as raised:
        paths_module.expand_path(url)

    assert str(raised.value) == "Invalid path: a file URL must have a valid host"


# Expected values are Node's fileURLToPath (WHATWG URL parsing), which Pi uses.
@pytest.mark.parametrize(
    ("windows", "url", "expected"),
    [
        (False, "file:///tmp/a%20b.txt", "/tmp/a b.txt"),
        # The WHATWG URL parser's steps: a backslash is a separator, C0 controls and spaces at the ends go.
        (False, "file:///tmp\\x", "/tmp/x"),
        (False, "file:///tmp/a ", "/tmp/a"),
        (False, "file:///tmp/a\x1f \x00", "/tmp/a"),
        (False, "file:///tmp/a%20", "/tmp/a "),
        (True, "file:///C:\\tmp\\x", "C:\\tmp\\x"),
        (True, "file://localhost\\C:\\x", "C:\\x"),
        # Pi's gate is case-sensitive: this is a relative path in Pi too.
        (False, "FILE:///tmp/a", "FILE:///tmp/a"),
        # IDNA maps a soft hyphen to nothing; Node fails a host with a zero-width joiner.
        (False, "file://local\u00adhost/x", "/x"),
        (False, "file://local\u200dhost/etc/passwd", ToolInputError("Invalid path: a file URL must have a valid host")),
        (True, "file://Server/Share/x", "\\\\server\\Share\\x"),
        (True, "file://192.168.1.5/x", "\\\\192.168.1.5\\x"),
        (True, "file://[::1]/x", "\\\\[::1]\\x"),
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
