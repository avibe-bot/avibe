"""The user's instruction files in the Agent's system prompt (C-7 ``tools.md`` section 9).

Ported from Pi (MIT, Copyright (c) 2025 Mario Zechner) at ``7fbbd5f``:
- ``packages/coding-agent/src/core/resource-loader.ts``: ``loadProjectContextFiles`` and ``findShadowedContextFile``;
- ``core/footer-data-provider.ts``: ``findGitPaths``;
- ``core/system-prompt.ts``: ``renderProjectContext``.

Avibe's deviations (section 9): one global path, a shared byte limit with its notices, one load per real path, blank
files left out, unreadable files skipped silently, and a precedence line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from core.agent_core.harness.context import escape

#: What each directory may hold, in order: the first readable one is that directory's file.
CANDIDATES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")
#: The bytes the whole section may carry (64 KiB): its header, each file's path and markup, the files' text, and the
#: notices. Counting all of it bounds the section by construction, however many files or however long their paths.
LIMIT = 65_536
#: Of ``LIMIT``, the bytes kept for the notice naming the files left out.
OMITTED_ROOM = 1_024
HEADER = "Project-specific instructions and guidelines:"
PRECEDENCE = "More general files come first; where two conflict, the later, more specific one wins."
#: Files below the cwd are never loaded; as Codex does, the section tells the Agent to look for them. Constant, and
#: present even when no file loaded, since a subtree may still hold one.
SUBTREE = (
    "A directory below the working directory may hold its own instruction file with more specific rules for that "
    "subtree: the first that exists of AGENTS.override.md, AGENTS.md, AGENTS.MD, CLAUDE.md, CLAUDE.MD, the others in "
    "that directory ignored. Read it before changing files there."
)
TRUNCATED = (
    "[Truncated: the first {shown} of {size} bytes. The instruction files share a 65,536-byte limit; read the file "
    "for the rest.]"
)
NOT_LOADED = "[Not loaded, over the 65,536-byte limit for instruction files: {files}. Read them if the task needs them.]"
MORE = "and {count} more"


@dataclass(frozen=True)
class InstructionFile:
    path: str
    #: What was read: at most the limit's worth of the file, so a huge one costs no more than a full section.
    content: str
    is_global: bool = False
    #: The file's size in bytes, without a byte-order mark; None means the size of ``content``.
    size: Optional[int] = None

    @property
    def byte_size(self) -> int:
        return self.size if self.size is not None else len(self.content.encode("utf-8"))


_BOM = b"\xef\xbb\xbf"
#: Git writes one path line into ``.git`` and ``commondir``: no more is read of either.
_GIT_LINE = 8192


def _read(path: str) -> Optional[tuple[str, int]]:
    """A candidate's text and size, or None when it is not a readable file. Symlinks are followed.

    Reads no more than the section can hold (plus a character's worth, so a cut still falls on a whole one): every
    Turn reads the files, and a mistaken multi-gigabyte one must cost no more than a full section.
    """
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            data = handle.read(len(_BOM) + LIMIT + 4)
    except OSError:
        return None
    if data.startswith(_BOM):
        data, size = data[len(_BOM) :], size - len(_BOM)
    text = data[: LIMIT + 4].decode("utf-8", "replace")
    # Read whole, its size is the text's: each undecodable byte became a three-byte replacement character.
    return text, (len(text.encode("utf-8")) if len(data) >= size else size)


def _from_dir(directory: str) -> Optional[InstructionFile]:
    for name in CANDIDATES:
        path = os.path.join(directory, name)
        read = _read(path)
        if read is not None:
            return InstructionFile(path, read[0], size=read[1])
    return None


def _git_paths(cwd: str) -> Optional[tuple[str, str]]:
    """The checkout root holding ``cwd`` and its common git directory, found by walking up (Pi's ``findGitPaths``)."""
    directory = cwd
    while True:
        marker = os.path.join(directory, ".git")
        try:
            if os.path.isfile(marker):
                with open(marker, encoding="utf-8", errors="replace") as handle:
                    content = handle.read(_GIT_LINE).strip()
                if content.startswith("gitdir: "):
                    git_dir = os.path.normpath(os.path.join(directory, content[len("gitdir: ") :].strip()))
                    if not os.path.exists(os.path.join(git_dir, "HEAD")):
                        return None
                    common = os.path.join(git_dir, "commondir")
                    if os.path.exists(common):
                        with open(common, encoding="utf-8", errors="replace") as handle:
                            return directory, os.path.normpath(os.path.join(git_dir, handle.read(_GIT_LINE).strip()))
                    return directory, git_dir
            elif os.path.isdir(marker):
                return (directory, marker) if os.path.exists(os.path.join(marker, "HEAD")) else None
        except OSError:
            return None
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _shadowed(cwd: str) -> Optional[tuple[str, str]]:
    """Where the main repository's file sits that a nested linked worktree's own one stands in for (Pi's
    ``findShadowedContextFile``): both are that repository's, so loading both would apply it twice.

    A place (the main checkout's real directory and the file's name), not a file: the two copies may be links to the
    same file, and the worktree's must still load."""
    found = _git_paths(cwd)
    if found is None:
        return None
    worktree_root, common = os.path.realpath(found[0]), os.path.realpath(found[1])
    main_root = os.path.dirname(common)
    # An ordinary checkout (the two are the same directory) or a sibling worktree, whose main one is no ancestor.
    if not worktree_root.startswith(main_root + os.sep):
        return None
    # A bare layout or a submodule: the directory above the git directory checks nothing out.
    if os.path.realpath(os.path.join(main_root, ".git")) != common:
        return None
    own = _from_dir(worktree_root)
    return (main_root, os.path.basename(own.path)) if own is not None else None


def load_instruction_files(cwd: str, global_path: Path) -> list[InstructionFile]:
    """The global file, then each directory's from the filesystem root down to ``cwd``.

    A file is loaded once per real path: as the global file, or else at the directory nearest ``cwd`` that reaches it.
    A blank file is left out.
    """
    files: list[InstructionFile] = []
    seen: set[str] = set()
    read = _read(str(global_path))
    if read is not None:
        seen.add(os.path.realpath(global_path))
        if read[0].strip():
            files.append(InstructionFile(str(global_path), read[0], is_global=True, size=read[1]))
    if not cwd:
        return files
    directory = os.path.abspath(cwd)
    shadowed = _shadowed(directory)
    chain: list[InstructionFile] = []
    while True:
        found = _from_dir(directory)
        real = os.path.realpath(found.path) if found is not None else None
        at_shadow = found is not None and (os.path.realpath(directory), os.path.basename(found.path)) == shadowed
        if found is not None and not at_shadow and real not in seen:
            seen.add(real)
            if found.content.strip():
                chain.insert(0, found)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return files + chain


def _cut(text: str, size: int) -> str:
    """The first ``size`` bytes of ``text`` in UTF-8, on a character boundary."""
    return text.encode("utf-8")[:size].decode("utf-8", "ignore")


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _attribute(path: str) -> str:
    """A path as a quoted attribute's value: ``escape``d, then its quote and ampersand as ``\\u0022`` and
    ``\\u0026``. Still injective: ``escape`` doubles every backslash, so only an escape writes ``\\u``."""
    return escape(path).replace('"', "\\u0022").replace("&", "\\u0026")


def _not_loaded(files: Sequence[InstructionFile]) -> str:
    """The notice naming the files left out, as many as fit ``OMITTED_ROOM``, then how many more."""
    named: list[str] = []
    for file in files:
        entries = [*named, f"{escape(file.path)} ({file.byte_size} bytes)"]
        rest = len(files) - len(entries)
        listed = "; ".join([*entries, MORE.format(count=rest)] if rest else entries)
        if _bytes("\n\n" + NOT_LOADED.format(files=listed)) > OMITTED_ROOM:
            break
        named = entries
    rest = len(files) - len(named)
    return "\n\n" + NOT_LOADED.format(files="; ".join([*named, MORE.format(count=rest)] if rest else named))


def project_context(files: Sequence[InstructionFile]) -> str:
    """The ``<project_context>`` section, at most ``LIMIT`` bytes, all of it counted; with no file, only ``SUBTREE``.

    The room goes first to the global file, then to the files from the cwd upward, so the most specific one is the last
    cut. A file that gets no room is named in the closing notice. The section keeps the files' order either way.
    """
    if not files:
        return f"<project_context>\n{SUBTREE}\n</project_context>"
    opening, closing = f"<project_context>\n{HEADER}\n{PRECEDENCE}\n{SUBTREE}", "\n</project_context>"
    left = LIMIT - _bytes(opening) - _bytes(closing) - OMITTED_ROOM
    first = [index for index, file in enumerate(files) if file.is_global]
    rest = [index for index in reversed(range(len(files))) if not files[index].is_global]
    blocks: dict[int, str] = {}
    for index in first + rest:
        file = files[index]
        start, end = f'\n\n<project_instructions path="{_attribute(file.path)}">\n', "\n</project_instructions>"
        room = left - _bytes(start) - _bytes(end)
        text, size = file.content, file.byte_size
        if size > room or _bytes(text) > room:
            # The notice's own bytes come out of the room first; its count can only shrink after the cut.
            room -= _bytes("\n" + TRUNCATED.format(shown=max(room, 0), size=size))
            if room <= 0:
                continue
            shown = _cut(text, room)
            text = f"{shown}\n{TRUNCATED.format(shown=_bytes(shown), size=size)}"
        blocks[index] = start + text + end
        left -= _bytes(blocks[index])
    omitted = [file for index, file in enumerate(files) if index not in blocks]
    body = "".join(blocks[index] for index in sorted(blocks))
    return opening + body + (_not_loaded(omitted) if omitted else "") + closing
