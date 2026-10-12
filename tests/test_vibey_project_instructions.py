"""The user's instruction files in Vibey's system prompt (C-7 ``agent-core-contracts/tools.md`` section 9).

Discovery runs on real temporary directories. The walk reaches the filesystem root, so each test keeps only the files
under its own temporary directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from modules.agents.vibey.instructions import InstructionFile, load_instruction_files, project_context

# The model-facing text, stated here rather than read from the constants it checks (C-7 tools.md section 9).
HEADER = (
    "Project-specific instructions and guidelines:\n"
    "More general files come first; where two conflict, the later, more specific one wins."
)


def truncated(shown: int, size: int) -> str:
    return (
        f"[Truncated: the first {shown} of {size} bytes. The instruction files share a 65,536-byte limit; read the "
        "file for the rest.]"
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _found(cwd: Path, global_path: Path, root: Path) -> list[tuple[str, str]]:
    """What the walk loads from ``cwd``, as paths relative to ``root`` (the global file by name), with contents."""
    loaded = []
    for file in load_instruction_files(str(cwd), global_path):
        if file.is_global:
            loaded.append(("<global>", file.content))
        elif Path(file.path).is_relative_to(root):
            loaded.append((Path(file.path).relative_to(root).as_posix(), file.content))
    return loaded


def test_the_global_file_comes_first_then_each_directory_from_the_root_down_to_the_cwd(tmp_path) -> None:
    global_path = _write(tmp_path / "home/.avibe/AGENTS.md", "global")
    _write(tmp_path / "work/AGENTS.md", "workspace")
    _write(tmp_path / "work/app/CLAUDE.md", "app")
    cwd = tmp_path / "work/app/src"
    cwd.mkdir(parents=True)

    assert _found(cwd, global_path, tmp_path) == [
        ("<global>", "global"),
        ("work/AGENTS.md", "workspace"),
        ("work/app/CLAUDE.md", "app"),
    ]
    # No global file: the walk alone.
    assert _found(cwd, tmp_path / "missing/AGENTS.md", tmp_path) == [
        ("work/AGENTS.md", "workspace"),
        ("work/app/CLAUDE.md", "app"),
    ]


def test_each_directory_contributes_its_first_candidate_and_the_override_wins(tmp_path) -> None:
    for name in ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md"):
        _write(tmp_path / "a" / name, f"a {name}")
    for name in ("AGENTS.md", "CLAUDE.md"):
        _write(tmp_path / "a/b" / name, f"b {name}")
    _write(tmp_path / "a/b/c/CLAUDE.md", "c CLAUDE.md")

    assert _found(tmp_path / "a/b/c", tmp_path / "none", tmp_path) == [
        ("a/AGENTS.override.md", "a AGENTS.override.md"),
        ("a/b/AGENTS.md", "b AGENTS.md"),
        ("a/b/c/CLAUDE.md", "c CLAUDE.md"),
    ]


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions that bind the user")
def test_an_unreadable_candidate_is_skipped_silently_and_the_next_one_read(tmp_path) -> None:
    locked = _write(tmp_path / "repo/AGENTS.md", "locked")
    _write(tmp_path / "repo/CLAUDE.md", "readable")
    (tmp_path / "other/AGENTS.md").mkdir(parents=True)  # not a file
    _write(tmp_path / "other/CLAUDE.md", "after a directory")
    locked.chmod(0)
    try:
        assert _found(tmp_path / "repo", tmp_path / "none", tmp_path) == [("repo/CLAUDE.md", "readable")]
        assert _found(tmp_path / "other", tmp_path / "none", tmp_path) == [("other/CLAUDE.md", "after a directory")]
    finally:
        locked.chmod(0o644)


def test_symlinks_are_followed_and_a_file_is_loaded_once(tmp_path) -> None:
    shared = _write(tmp_path / "elsewhere/principles.md", "principles")
    global_path = tmp_path / "home/.avibe/AGENTS.md"
    global_path.parent.mkdir(parents=True)
    global_path.symlink_to(shared)
    # The repository links its file to the same one, and keeps CLAUDE.md as a link to AGENTS.md.
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo/AGENTS.md").symlink_to(shared)
    (tmp_path / "repo/CLAUDE.md").symlink_to(tmp_path / "repo/AGENTS.md")
    # A file outside the chain, reached through a link in it, is read.
    (tmp_path / "repo/app").mkdir()
    (tmp_path / "repo/app/AGENTS.md").symlink_to(_write(tmp_path / "elsewhere/app.md", "app rules"))

    assert _found(tmp_path / "repo/app", global_path, tmp_path) == [
        ("<global>", "principles"),
        ("repo/app/AGENTS.md", "app rules"),
    ]


def test_blank_files_are_left_out(tmp_path) -> None:
    _write(tmp_path / "repo/AGENTS.md", "  \n\n")
    _write(tmp_path / "repo/CLAUDE.md", "not reached: the blank AGENTS.md is this directory's file")
    _write(tmp_path / "repo/app/AGENTS.md", "\ufeffapp")

    assert _found(tmp_path / "repo/app", tmp_path / "none", tmp_path) == [("repo/app/AGENTS.md", "app")]


def _git_repo(root: Path) -> None:
    _write(root / ".git/HEAD", "ref: refs/heads/master\n")


def _linked_worktree(main: Path, worktree: Path, name: str) -> None:
    _write(main / ".git/worktrees" / name / "HEAD", "ref: refs/heads/feature\n")
    _write(main / ".git/worktrees" / name / "commondir", "../..\n")
    _write(worktree / ".git", f"gitdir: {main / '.git/worktrees' / name}\n")


@pytest.mark.parametrize("main_is_a_link", (False, True), ids=("main file", "main file is a link"))
def test_a_nested_linked_worktree_does_not_load_its_main_repository_file_twice(tmp_path, main_is_a_link) -> None:
    main = tmp_path / "repo"
    _git_repo(main)
    if main_is_a_link:
        (main / "AGENTS.md").symlink_to(_write(tmp_path / "elsewhere/main-rules.md", "main copy"))
    else:
        _write(main / "AGENTS.md", "main copy")
    nested = main / ".worktrees/feature"
    _linked_worktree(main, nested, "feature")
    _write(nested / "AGENTS.md", "worktree copy")
    sibling = tmp_path / "feature-sibling"
    _linked_worktree(main, sibling, "sibling")
    _write(sibling / "AGENTS.md", "sibling copy")

    # Both copies are the repository's: only the worktree's own is loaded.
    assert _found(nested / "src", tmp_path / "none", tmp_path) == [("repo/.worktrees/feature/AGENTS.md", "worktree copy")]
    # A sibling worktree has no main repository above it, and an ordinary checkout is its own repository.
    assert _found(sibling, tmp_path / "none", tmp_path) == [("feature-sibling/AGENTS.md", "sibling copy")]
    assert _found(main / "src", tmp_path / "none", tmp_path) == [("repo/AGENTS.md", "main copy")]


def test_the_section_holds_each_file_by_path_in_order() -> None:
    files = [
        InstructionFile("/home/me/.avibe/AGENTS.md", "global rules", is_global=True),
        InstructionFile("/src/<app>/AGENTS.md", "project rules"),
    ]

    assert project_context(files) == (
        f"<project_context>\n{HEADER}\n\n"
        '<project_instructions path="/home/me/.avibe/AGENTS.md">\nglobal rules\n</project_instructions>\n\n'
        '<project_instructions path="/src/\\u003capp\\u003e/AGENTS.md">\nproject rules\n</project_instructions>\n'
        "</project_context>"
    )
    assert project_context([]) == ""


def test_the_files_share_64_kib_given_first_to_the_global_file_then_from_the_cwd_upward() -> None:
    global_file = InstructionFile("/g/AGENTS.md", "g" * 10, is_global=True)
    root_file = InstructionFile("/AGENTS.md", "r" * 40_000)

    # The root file is cut: the global file and the cwd's come first.
    section = project_context([global_file, root_file, InstructionFile("/repo/AGENTS.md", "p" * 40_000)])
    assert "g" * 10 + "\n</project_instructions>" in section and "p" * 40_000 + "\n</project_instructions>" in section
    assert "r" * 25_526 + "\n" + truncated(25_526, 40_000) + "\n</project_instructions>" in section
    assert section.index("/g/AGENTS.md") < section.index('"/AGENTS.md"') < section.index("/repo/AGENTS.md")

    # Less room: the cwd's file is cut, and the root file is named as not loaded.
    section = project_context([global_file, root_file, InstructionFile("/repo/AGENTS.md", "p" * 70_000)])
    assert '"/AGENTS.md"' not in section and "p" * 65_526 + "\n" + truncated(65_526, 70_000) in section
    assert section.endswith(
        "[Not loaded, over the 65,536-byte limit for instruction files: /AGENTS.md (40000 bytes). Read them if the "
        "task needs them.]\n</project_context>"
    )


def test_a_cut_falls_on_a_character_boundary() -> None:
    # One byte goes to the global file, leaving an odd 65,535 for a file of two-byte characters.
    files = [InstructionFile("/g/AGENTS.md", "g", is_global=True), InstructionFile("/AGENTS.md", "é" * 40_000)]

    assert "é" * 32_767 + "\n" + truncated(65_534, 80_000) in project_context(files)


def test_a_huge_file_is_read_only_as_far_as_the_section_can_hold(tmp_path) -> None:
    # Every Turn reads the files: a mistaken multi-megabyte one must cost no more than a full section.
    _write(tmp_path / "repo/AGENTS.md", "x" * (16 * 65_536))

    (file,) = [found for found in load_instruction_files(str(tmp_path / "repo"), tmp_path / "none") if
               Path(found.path).is_relative_to(tmp_path)]
    assert len(file.content.encode("utf-8")) <= 65_536 + 4
    assert truncated(65_536, 16 * 65_536) in project_context([file])
