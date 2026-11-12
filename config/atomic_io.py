"""One owner for publishing a complete local state document atomically.

Nineteen modules used to hand-roll "write a temp file, then ``os.replace`` it
over the destination", and no two of them hand-rolled it quite the same way.
Most of the variation was harmless drift — some fsynced the payload, most did
not, almost none fsynced the parent directory — but two call sites had shipped
a real defect nobody had noticed, because each one only ever had to satisfy its
own test:

* ``core/managed_runtime`` and ``vibe/codex_config`` both derived a fixed
  ``<name>.tmp`` path, so two concurrent writers share one temp file and one
  can ``os.replace`` the other's half-flushed bytes onto the destination.
* ``vibe/codex_config`` wrote that temp file under the ambient umask and
  tightened the *destination* after the rename, leaving a window in which a
  Codex auth file is world-readable — permanent if the process dies in between.

So this module owns the swap and the callers own the bytes. Serialization
formats stay where they are (``indent=2`` for human-read state, compact for
machine-read projections, ``sort_keys`` for diffable manifests): those are real
per-file decisions, unlike the durability and permission gaps above.

**Scope.** This owns the mechanics of replacing one *whole* state document.
It is deliberately not the owner of every rename in the repository, and
several callers stay outside it on purpose: a compare-and-swap that re-reads
before replacing (``config/v2_config``), an editor writing *user* files whose
existing mode and mtime must survive (``core/file_browser_service``), a
database swap that must move sidecars with the file (``storage/backups``), and
anything reserving a temp *name* without writing content (``vibe/screenshot``).
Reach for this when the write is "these bytes, entirely, or nothing".
Native migration's user-file transaction delegates publication here with an
explicit captured mode and a final consent check, retaining ownership of its
compare-before-write, strict directory durability and recovery policy.

The guarantees, for every caller:

* A reader never observes a partial write. The payload lands in a temp file
  that is fsynced before it is renamed over the destination.
* Two writers never collide. ``tempfile.mkstemp`` allocates a distinct temp
  name per call, so overlapping writers publish whole payloads rather than
  each other's fragments.
* A failed write never leaves a temp file behind, and never truncates the
  destination — the old contents survive until the rename succeeds.
* On POSIX the default replacement is owner-private (0600) from creation through
  publication. Windows keeps the temporary file's creation/inherited ACL;
  chmod's mode bits are not a private DACL. A user-file editor may supply its captured mode;
  that mode is applied only to the unpublished inode, before its fsync.
  No caller widens a published path through this primitive.
"""

from __future__ import annotations

import io
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, overload

__all__ = ["open_snapshot", "read_snapshot", "replace_snapshot", "write_atomic"]


def open_snapshot(path: Path) -> BinaryIO:
    """Open one immutable snapshot without obstructing a cooperating publisher.

    Windows needs both a delete-sharing reader and the POSIX rename semantics
    in ``replace_snapshot``. No application lock is taken: callers may already
    hold a SQLite writer while another process holds the config writer lock.
    """
    if os.name != "nt":
        return path.open("rb")
    from core.agent_core.tools import windows_files

    descriptor = windows_files.open_for_reading(os.fspath(path))
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


@overload
def read_snapshot(path: Path, *, encoding: None = None) -> bytes: ...


@overload
def read_snapshot(path: Path, *, encoding: str) -> str: ...


def read_snapshot(path: Path, *, encoding: str | None = None) -> bytes | str:
    """Read a whole snapshot, retaining ``read_text`` newline handling for text."""
    with open_snapshot(path) as handle:
        if encoding is None:
            return handle.read()
        with io.TextIOWrapper(handle, encoding=encoding) as text:
            return text.read()


def replace_snapshot(source: str | Path, destination: Path) -> None:
    """Publish a prepared snapshot; preparation, guards and locks stay with callers.

    Unsupported Windows filesystems retain the existing helper's ``os.replace``
    fallback, which can fail with an open reader. Access errors propagate; there
    is no retry, destination unlink, or permission relaxation.
    """
    if os.name == "nt":
        from core.agent_core.tools import windows_files

        windows_files.replace(os.fspath(source), os.fspath(destination))
    else:
        os.replace(source, destination)


def _fsync_directory(path: Path) -> None:
    """Best-effort durability for a replaced directory entry."""

    flags = getattr(os, "O_DIRECTORY", None)
    if flags is None:
        return
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_atomic(
    path: Path,
    data: str | bytes,
    *,
    follow_symlinks: bool = False,
    mode: int = 0o600,
    before_replace: Callable[[], None] | None = None,
) -> None:
    """Replace *path* with *data* atomically, creating parent directories.

    ``follow_symlinks`` writes *through* a symlink to its real target instead of
    replacing the link with a regular file. Needed where a user may have linked
    a file into a shared dotfiles repository (``~/.claude/CLAUDE.md`` ->
    ``~/dotfiles/CLAUDE.md``): replacing the link would silently break that
    setup. Off by default, because for state this process owns, resolving a
    symlink an attacker planted is the wrong answer.

    ``mode`` is an explicit publication mode, not a request to inherit the
    destination's current permissions. Agent-owned state keeps the 0600 default.
    ``before_replace`` may reject stale caller-owned consent after the temporary
    bytes and permissions are flushed, immediately before publication. This is
    a comparison hook, not a cross-process atomic compare-and-swap guarantee.

    Failures before publication leave the destination untouched by this writer
    and remove the unpublished temporary file. Callback exceptions propagate.
    """

    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
        raise ValueError("invalid publication mode")
    target = Path(os.path.realpath(path)) if follow_symlinks else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data

    # mkstemp starts owner-private. Its creation mode is still filtered by
    # umask, so set the exact publication mode on the unpublished inode.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    unpublished: str | None = temporary_name
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            else:  # pragma: no cover - platform permission fallback
                os.chmod(temporary_name, mode)
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        replace_snapshot(temporary_name, target)
        unpublished = None
        _fsync_directory(target.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if unpublished is not None:
            try:
                os.unlink(unpublished)
            except OSError:  # pragma: no cover - best effort cleanup
                pass
