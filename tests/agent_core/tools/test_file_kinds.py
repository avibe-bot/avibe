"""The OS-edge table in the PR: every kind of file the file tools can meet, and the defined result for each.

A row that would block (a FIFO) or replace something that is not a regular file must not; every
other row gets one error text, written the same way for every kind.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import stat
import tempfile

import pytest

from core.agent_core.tools.edit import EditTool
from core.agent_core.tools.read import ReadTool
from core.agent_core.tools.write import WriteTool
from tests.agent_core.tools.conftest import result_text

KINDS = [
    "regular",
    "read_only",
    "unreadable",
    "directory",
    "fifo",
    "socket",
    "device",
    "link_regular",
    "link_directory",
    "link_device",
    "dangling",
    "missing",
    "loop",
    "under_file",
    "in_locked_dir",
]
NOT_REGULAR = {"fifo", "socket", "device", "link_device"}
DIRECTORY = {"directory", "link_directory"}
ABSENT = {"dangling", "missing"}
NOT_ROOT = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")


@pytest.fixture
def place(tmp_path):
    """Build one kind of file and return the path a model would pass for it."""
    cleanups = []

    def make(kind: str) -> str:
        if kind in ("regular", "read_only", "unreadable"):
            (tmp_path / "f").write_text("hello\n")
            mode = {"regular": 0o644, "read_only": 0o444, "unreadable": 0o000}[kind]
            os.chmod(tmp_path / "f", mode)
            cleanups.append(lambda: os.chmod(tmp_path / "f", 0o644))
            return "f"
        if kind == "directory":
            (tmp_path / "d").mkdir()
            return "d"
        if kind == "fifo":
            os.mkfifo(tmp_path / "p")
            return "p"
        if kind == "socket":
            # Unix socket paths are short; a private directory under /tmp keeps it under the limit.
            directory = tempfile.mkdtemp(prefix="avs", dir="/tmp")
            server = socket.socket(socket.AF_UNIX)
            server.bind(os.path.join(directory, "s"))
            cleanups.append(server.close)
            cleanups.append(lambda: shutil.rmtree(directory, ignore_errors=True))
            return os.path.join(directory, "s")
        if kind == "device":
            return os.devnull
        if kind == "link_regular":
            (tmp_path / "f").write_text("hello\n")
            (tmp_path / "l").symlink_to(tmp_path / "f")
            return "l"
        if kind == "link_directory":
            (tmp_path / "d").mkdir()
            (tmp_path / "l").symlink_to(tmp_path / "d")
            return "l"
        if kind == "link_device":
            (tmp_path / "l").symlink_to(os.devnull)
            return "l"
        if kind == "dangling":
            (tmp_path / "l").symlink_to(tmp_path / "missing" / "f")
            return "l"
        if kind == "missing":
            return "missing/sub/f"
        if kind == "under_file":
            (tmp_path / "f").write_text("hello\n")
            return "f/x"
        if kind == "in_locked_dir":
            # A writable file in a directory this user cannot add to.
            (tmp_path / "ro").mkdir()
            (tmp_path / "ro" / "f").write_text("hello\n")
            os.chmod(tmp_path / "ro", 0o555)
            cleanups.append(lambda: os.chmod(tmp_path / "ro", 0o755))
            return "ro/f"
        assert kind == "loop"
        (tmp_path / "l").symlink_to(tmp_path / "l2")
        (tmp_path / "l2").symlink_to(tmp_path / "l")
        return "l"

    yield make
    for cleanup in reversed(cleanups):
        cleanup()


async def _run(tool, arguments, ctx, tmp_path):
    """Run the tool; a FIFO that blocks it fails the row, and is unblocked so no thread is left behind."""
    task = asyncio.ensure_future(tool.execute(arguments, ctx))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except asyncio.TimeoutError:
        # Each blocked open of the FIFO needs a writer to come and go; give it one until the tool returns.
        fifo = tmp_path / "p"
        for _ in range(100):
            if task.done():
                break
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            await asyncio.sleep(0.05)
        pytest.fail("the tool blocked on a file it should not have opened")


def _cases(kinds):
    return [pytest.param(kind, marks=NOT_ROOT) if kind in ("unreadable", "in_locked_dir") else kind for kind in kinds]


@pytest.mark.parametrize("kind", _cases(KINDS))
async def test_read_has_a_defined_result_for_every_kind(tmp_path, make_ctx, place, kind):
    path = place(kind)

    result = await _run(ReadTool(), {"path": path}, make_ctx(), tmp_path)

    if kind in ("regular", "read_only", "link_regular", "in_locked_dir"):
        assert (result.is_error, result_text(result)) == (False, "hello\n")
        return
    reason = {
        "unreadable": "permission denied",
        "under_file": "not a directory",
        "loop": "too many levels of symbolic links",
        **dict.fromkeys(DIRECTORY, "it is a directory"),
        **dict.fromkeys(NOT_REGULAR, "it is not a regular file"),
        **dict.fromkeys(ABSENT, "no such file or directory"),
    }[kind]
    assert (result.is_error, result_text(result)) == (True, f"Cannot read {path}: {reason}.")


@pytest.mark.parametrize("kind", _cases(KINDS))
async def test_write_has_a_defined_result_for_every_kind(tmp_path, make_ctx, place, kind):
    path = place(kind)
    before = (
        os.lstat(path if os.path.isabs(path) else tmp_path / path).st_mode
        if kind not in ABSENT | {"under_file"}
        else None
    )

    result = await _run(WriteTool(), {"path": path, "content": "new\n"}, make_ctx(), tmp_path)

    if kind in ("regular", "link_regular", "dangling", "missing"):
        assert (result.is_error, result_text(result)) == (False, f"Successfully wrote to {path}")
        target = {"regular": "f", "link_regular": "f", "dangling": "missing/f", "missing": "missing/sub/f"}[kind]
        assert (tmp_path / target).read_text() == "new\n"
        if kind in ("link_regular", "dangling"):
            assert (tmp_path / path).is_symlink()
        return
    reason = {
        "read_only": "permission denied",
        "unreadable": "permission denied",
        # Replacing needs a temp file beside it; writing in place could leave it half-written (ENOSPC).
        "in_locked_dir": "its directory is not writable, so the file cannot be replaced safely",
        "under_file": "not a directory",
        "loop": "too many levels of symbolic links",
        **dict.fromkeys(DIRECTORY, "it is a directory"),
        **dict.fromkeys(NOT_REGULAR, "it is not a regular file"),
    }[kind]
    assert (result.is_error, result_text(result)) == (True, f"Cannot write {path}: {reason}.")
    # Nothing that is not a writable regular file is ever replaced.
    if before is not None:
        assert os.lstat(path if os.path.isabs(path) else tmp_path / path).st_mode == before
    if kind == "read_only":
        assert (tmp_path / "f").read_text() == "hello\n"
    if kind == "in_locked_dir":
        assert (tmp_path / "ro" / "f").read_text() == "hello\n"


@pytest.mark.parametrize("kind", _cases(KINDS))
async def test_edit_has_a_defined_result_for_every_kind(tmp_path, make_ctx, place, kind):
    path = place(kind)
    arguments = {"path": path, "edits": [{"oldText": "hello", "newText": "bye"}]}

    result = await _run(EditTool(), arguments, make_ctx(), tmp_path)

    if kind in ("regular", "link_regular"):
        assert (result.is_error, result_text(result)) == (False, f"Successfully replaced 1 block(s) in {path}.")
        assert (tmp_path / "f").read_text() == "bye\n"
        assert (tmp_path / path).is_symlink() == (kind == "link_regular")
        return
    detail = {
        "read_only": "Error code: EACCES.",
        "unreadable": "Error code: EACCES.",
        "in_locked_dir": "Its directory is not writable, so the file cannot be replaced safely.",
        "loop": "Error code: ELOOP.",
        "under_file": "Error code: ENOTDIR.",
        **dict.fromkeys(DIRECTORY, "Error code: EISDIR."),
        **dict.fromkeys(NOT_REGULAR, "It is not a regular file."),
        **dict.fromkeys(ABSENT, "Error code: ENOENT."),
    }[kind]
    assert (result.is_error, result_text(result)) == (True, f"Could not edit file: {path}. {detail}")
    if kind == "read_only":
        assert stat.S_IMODE(os.stat(tmp_path / "f").st_mode) == 0o444
    if kind == "in_locked_dir":
        assert (tmp_path / "ro" / "f").read_text() == "hello\n"
