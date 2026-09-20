"""Read/write each agent backend's *global* instructions file.

These are the user-level "global system prompt" files the agent CLIs read at
the start of every session, ahead of any project- or agent-specific prompt:

- ``claude``   -> ``~/.claude/CLAUDE.md``          (Claude Code "user memory")
- ``codex``    -> ``$CODEX_HOME/AGENTS.md``        (default ``~/.codex/AGENTS.md``)
- ``opencode`` -> ``~/.config/opencode/AGENTS.md`` (OpenCode global rules)

This is the *global* twin of :mod:`vibe.project_agents_md`, which edits a single
project's ``AGENTS.md``. The Web UI "Global prompts" dialog edits all three from
one place and can sync one backend's content over the others.

Path resolution reuses the existing per-backend home resolvers
(:func:`get_claude_home` / :func:`get_codex_home` / :func:`get_opencode_config_paths`)
so we always point at the directory the live CLI actually consults — including
``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` overrides.

The functions are deliberately pure filesystem helpers keyed by backend id,
accepting an optional ``home`` override so they stay unit-testable in isolation
(the HTTP layer passes none and they resolve the real user home).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from config.atomic_io import write_atomic
from modules.agents.catalog import AGENT_BACKENDS, is_agent_backend
from vibe.claude_config import get_claude_home
from vibe.codex_config import get_codex_home
from vibe.opencode_config import get_opencode_config_paths

# Per-backend global instruction filename. Claude reads its legacy ``CLAUDE.md``
# "user memory"; Codex and OpenCode both read ``AGENTS.md``.
GLOBAL_INSTRUCTION_FILENAME: dict[str, str] = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "opencode": "AGENTS.md",
}


def _opencode_global_dir(home: Path | None = None) -> Path:
    """Return the directory OpenCode reads its global ``AGENTS.md`` from.

    OpenCode's rules docs pin global rules to ``~/.config/opencode/AGENTS.md``
    (with a ``~/.claude/CLAUDE.md`` fallback) — *not* next to a legacy
    ``~/.opencode/opencode.json``. Editing ``~/.opencode/AGENTS.md`` would report
    a successful save while new sessions keep reading the unchanged
    ``~/.config/opencode/AGENTS.md``, so we always target the documented path
    (the first ``get_opencode_config_paths`` candidate's directory).
    """
    return get_opencode_config_paths(home)[0].parent


def global_instruction_path(backend: str, home: Path | None = None) -> Path:
    """Resolve the absolute path to *backend*'s global instructions file."""
    if backend == "claude":
        return get_claude_home(home) / GLOBAL_INSTRUCTION_FILENAME["claude"]
    if backend == "codex":
        return get_codex_home(home) / GLOBAL_INSTRUCTION_FILENAME["codex"]
    if backend == "opencode":
        return _opencode_global_dir(home) / GLOBAL_INSTRUCTION_FILENAME["opencode"]
    raise ValueError(f"Unsupported agent backend: {backend!r}")


def read_global_agents_md(backend: str, home: Path | None = None) -> dict:
    """Return the editor seed for *backend*'s global instructions file.

    ``exists`` is ``False`` (and ``content`` empty) when the file is absent, so
    the UI shows an empty editor and still surfaces the path the save will
    create.
    """
    path = global_instruction_path(backend, home)
    exists = path.is_file()
    content = ""
    read_error = False
    if exists:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # A non-UTF-8 or otherwise unreadable file must not 500 the whole
            # editor — one bad file would block every backend's tab. Surface it
            # so the UI can warn and refuse to overwrite it with an empty draft.
            read_error = True
    return {
        "backend": backend,
        "path": str(path),
        "filename": GLOBAL_INSTRUCTION_FILENAME[backend],
        "content": content,
        "exists": exists,
        "read_error": read_error,
    }


def read_all_global_agents_md(home: Path | None = None) -> list[dict]:
    """Return the editor seed for every known backend, in catalog order."""
    return [read_global_agents_md(backend, home) for backend in AGENT_BACKENDS]


def write_global_agents_md(backend: str, content: str, home: Path | None = None) -> dict:
    """Write *content* to *backend*'s global instructions file and re-read it."""
    path = global_instruction_path(backend, home)
    # ``follow_symlinks`` keeps a dotfiles setup intact: a user who links
    # ``~/.claude/CLAUDE.md`` -> ``~/dotfiles/CLAUDE.md`` expects an edit here to
    # land in the repo, not to replace the link with a regular file.
    write_atomic(path, content, follow_symlinks=True)
    return read_global_agents_md(backend, home)


def write_many_global_agents_md(
    backends: Iterable[str], content: str, home: Path | None = None
) -> list[dict]:
    """Write the same *content* to each backend in *backends*.

    Backs both per-backend Save (a single id) and one-click Sync (every id).
    All ids are validated before any write so a bad request cannot half-apply.
    Returns the refreshed seed for every backend so the UI can re-sync paths and
    ``exists`` flags after the write.
    """
    targets: list[str] = []
    for backend in backends:
        if not is_agent_backend(backend) or backend not in GLOBAL_INSTRUCTION_FILENAME:
            raise ValueError(f"Unsupported agent backend: {backend!r}")
        targets.append(backend)
    if not targets:
        raise ValueError("backends must not be empty")
    for backend in targets:
        write_global_agents_md(backend, content, home)
    return read_all_global_agents_md(home)
