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
#: The bytes of file content the section holds in total (64 KiB).
LIMIT = 65_536
HEADER = "Project-specific instructions and guidelines:"
PRECEDENCE = "More general files come first; where two conflict, the later, more specific one wins."
TRUNCATED = (
    "[Truncated: the first {shown} of {size} bytes. The instruction files share a 65,536-byte limit; read the file "
    "for the rest.]"
)
NOT_LOADED = "[Not loaded, over the 65,536-byte limit for instruction files: {files}. Read them if the task needs them.]"


@dataclass(frozen=True)
class InstructionFile:
    path: str
    content: str
    is_global: bool = False


def _read(path: str) -> Optional[str]:
    """A candidate's text, or None when it is not a readable file. Symlinks are followed."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    return text[1:] if text.startswith("\ufeff") else text


def _from_dir(directory: str) -> Optional[InstructionFile]:
    for name in CANDIDATES:
        path = os.path.join(directory, name)
        text = _read(path)
        if text is not None:
            return InstructionFile(path, text)
    return None


def _git_paths(cwd: str) -> Optional[tuple[str, str]]:
    """The checkout root holding ``cwd`` and its common git directory, found by walking up (Pi's ``findGitPaths``)."""
    directory = cwd
    while True:
        marker = os.path.join(directory, ".git")
        try:
            if os.path.isfile(marker):
                with open(marker, encoding="utf-8", errors="replace") as handle:
                    content = handle.read().strip()
                if content.startswith("gitdir: "):
                    git_dir = os.path.normpath(os.path.join(directory, content[len("gitdir: ") :].strip()))
                    if not os.path.exists(os.path.join(git_dir, "HEAD")):
                        return None
                    common = os.path.join(git_dir, "commondir")
                    if os.path.exists(common):
                        with open(common, encoding="utf-8", errors="replace") as handle:
                            return directory, os.path.normpath(os.path.join(git_dir, handle.read().strip()))
                    return directory, git_dir
            elif os.path.isdir(marker):
                return (directory, marker) if os.path.exists(os.path.join(marker, "HEAD")) else None
        except OSError:
            return None
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _shadowed(cwd: str) -> Optional[str]:
    """The main repository's file that a nested linked worktree's own one stands in for (Pi's
    ``findShadowedContextFile``): both are that repository's, so loading both would apply it twice. Real path."""
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
    return os.path.join(main_root, os.path.basename(own.path)) if own is not None else None


def load_instruction_files(cwd: str, global_path: Path) -> list[InstructionFile]:
    """The global file, then each directory's from the filesystem root down to ``cwd``.

    A file is loaded once per real path: as the global file, or else at the directory nearest ``cwd`` that reaches it.
    A blank file is left out.
    """
    files: list[InstructionFile] = []
    seen: set[str] = set()
    text = _read(str(global_path))
    if text is not None:
        seen.add(os.path.realpath(global_path))
        if text.strip():
            files.append(InstructionFile(str(global_path), text, is_global=True))
    if not cwd:
        return files
    directory = os.path.abspath(cwd)
    shadowed = _shadowed(directory)
    chain: list[InstructionFile] = []
    while True:
        found = _from_dir(directory)
        real = os.path.realpath(found.path) if found is not None else None
        if found is not None and real != shadowed and real not in seen:
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


def project_context(files: Sequence[InstructionFile], limit: int = LIMIT) -> str:
    """The ``<project_context>`` section, or "" with no file.

    The files share ``limit`` bytes: first the global file, then the files from the cwd upward, so the most specific
    one is the last cut. The section keeps the files' order either way.
    """
    if not files:
        return ""
    sizes = [len(file.content.encode("utf-8")) for file in files]
    first = [index for index, file in enumerate(files) if file.is_global]
    rest = [index for index in reversed(range(len(files))) if not files[index].is_global]
    allowed, left = [0] * len(files), limit
    for index in first + rest:
        allowed[index] = min(sizes[index], left)
        left -= allowed[index]
    blocks, omitted = [], []
    for file, size, room in zip(files, sizes, allowed):
        path = escape(file.path)
        if room == 0:
            omitted.append(f"{path} ({size} bytes)")
            continue
        content = file.content
        if room < size:
            shown = _cut(content, room)
            content = f"{shown}\n{TRUNCATED.format(shown=len(shown.encode('utf-8')), size=size)}"
        blocks.append(f'<project_instructions path="{path}">\n{content}\n</project_instructions>')
    if omitted:
        blocks.append(NOT_LOADED.format(files="; ".join(omitted)))
    body = "\n\n".join([f"{HEADER}\n{PRECEDENCE}", *blocks])
    return f"<project_context>\n{body}\n</project_context>"
