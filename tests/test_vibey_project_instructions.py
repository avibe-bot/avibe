"""The user's instruction files in Vibey's system prompt (C-7 ``agent-core-contracts/tools.md`` section 9).

Discovery runs on real temporary directories. The walk reaches the filesystem root, so each test keeps only the files
under its own temporary directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from modules.agents.vibey.instructions import InstructionFile, load_instruction_files, project_context

# The model-facing text, stated here rather than read from the constants it checks (C-7 tools.md section 9).
SUBTREE = (
    "A directory below the working directory may hold its own instruction file (AGENTS.override.md, AGENTS.md, or "
    "CLAUDE.md) with more specific rules for that subtree: read it before changing files there."
)
HEADER = (
    "Project-specific instructions and guidelines:\n"
    f"More general files come first; where two conflict, the later, more specific one wins.\n{SUBTREE}"
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


@pytest.mark.parametrize("links", ("none", "main", "both"), ids=("files", "main file is a link", "both link one file"))
def test_a_nested_linked_worktree_does_not_load_its_main_repository_file_twice(tmp_path, links) -> None:
    main = tmp_path / "repo"
    _git_repo(main)
    nested = main / ".worktrees/feature"
    _linked_worktree(main, nested, "feature")
    if links == "both":
        # Both checkouts link the same shared file: it is still the worktree's that loads.
        shared = _write(tmp_path / "elsewhere/rules.md", "shared rules")
        (main / "AGENTS.md").symlink_to(shared)
        (nested / "AGENTS.md").symlink_to(shared)
        assert _found(nested / "src", tmp_path / "none", tmp_path) == [
            ("repo/.worktrees/feature/AGENTS.md", "shared rules")
        ]
        return
    if links == "main":
        (main / "AGENTS.md").symlink_to(_write(tmp_path / "elsewhere/main-rules.md", "main copy"))
    else:
        _write(main / "AGENTS.md", "main copy")
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
        InstructionFile('/src/<a "b" & c>/AGENTS.md', "project rules"),
    ]

    # A path is escaped for its quoted attribute: it cannot close the attribute or the tag.
    assert project_context(files) == (
        f"<project_context>\n{HEADER}\n\n"
        '<project_instructions path="/home/me/.avibe/AGENTS.md">\nglobal rules\n</project_instructions>\n\n'
        '<project_instructions path="/src/\\u003ca \\u0022b\\u0022 \\u0026 c\\u003e/AGENTS.md">\nproject rules\n'
        "</project_instructions>\n</project_context>"
    )
    # Files below the cwd are never loaded, so the rule to look for them stands even when no file loaded.
    assert project_context([]) == f"<project_context>\n{SUBTREE}\n</project_context>"


_NOTICE = re.compile(
    r"\n\[Truncated: the first (\d+) of (\d+) bytes\. The instruction files share a 65,536-byte limit; read the file "
    r"for the rest\.\]\n</project_instructions>"
)


def _blocks(section: str) -> dict[str, str]:
    """Each file's block text by path."""
    return dict(re.findall(r'<project_instructions path="([^"]*)">\n(.*?)\n</project_instructions>', section, re.S))


def test_the_section_carries_at_most_64_kib_given_first_to_the_global_file_then_from_the_cwd_upward() -> None:
    global_file = InstructionFile("/g/AGENTS.md", "g" * 10, is_global=True)
    root_file = InstructionFile("/AGENTS.md", "r" * 40_000)

    # The root file is cut: the global file and the cwd's come first.
    section = project_context([global_file, root_file, InstructionFile("/repo/AGENTS.md", "p" * 40_000)])
    blocks = _blocks(section)
    assert len(section.encode("utf-8")) <= 65_536
    assert blocks["/g/AGENTS.md"] == "g" * 10 and blocks["/repo/AGENTS.md"] == "p" * 40_000
    ((shown, size),) = _NOTICE.findall(section)
    assert size == "40000" and blocks["/AGENTS.md"].startswith("r" * int(shown) + "\n[Truncated")
    assert section.index("/g/AGENTS.md") < section.index('"/AGENTS.md"') < section.index("/repo/AGENTS.md")

    # Less room: the cwd's file is cut, and the root file is named as not loaded.
    section = project_context([global_file, root_file, InstructionFile("/repo/AGENTS.md", "p" * 70_000)])
    assert len(section.encode("utf-8")) <= 65_536 and '"/AGENTS.md"' not in section
    ((shown, size),) = _NOTICE.findall(section)
    assert size == "70000" and _blocks(section)["/repo/AGENTS.md"].startswith("p" * int(shown) + "\n[Truncated")
    assert section.endswith(
        "[Not loaded, over the 65,536-byte limit for instruction files: /AGENTS.md (40000 bytes). Read them if the "
        "task needs them.]\n</project_context>"
    )


def test_paths_and_markup_count_toward_the_limit() -> None:
    # A deep tree with a file at every level: each block repeats a long path, and the notice names what it can.
    files = [InstructionFile("/" + "d/" * 100 + f"{index}/AGENTS.md", "x") for index in range(1_000)]

    section = project_context(files)
    assert len(section.encode("utf-8")) <= 65_536
    assert re.search(r"; and \d+ more\. Read them if the task needs them\.\]\n</project_context>$", section)


def test_a_cut_falls_on_a_character_boundary() -> None:
    section = project_context([InstructionFile("/AGENTS.md", "é" * 40_000)])

    ((shown, size),) = _NOTICE.findall(section)
    assert size == "80000" and _blocks(section)["/AGENTS.md"].startswith("é" * (int(shown) // 2) + "\n[Truncated")
    assert int(shown) % 2 == 0


def test_a_huge_file_is_read_only_as_far_as_the_section_can_hold(tmp_path) -> None:
    # Every Turn reads the files: a mistaken multi-megabyte one must cost no more than a full section.
    _write(tmp_path / "repo/AGENTS.md", "x" * (16 * 65_536))

    (file,) = [found for found in load_instruction_files(str(tmp_path / "repo"), tmp_path / "none") if
               Path(found.path).is_relative_to(tmp_path)]
    assert len(file.content.encode("utf-8")) <= 65_536 + 4
    ((_, size),) = _NOTICE.findall(project_context([file]))
    assert size == str(16 * 65_536)


def test_undecodable_bytes_count_toward_the_limit_as_read(tmp_path) -> None:
    # Each invalid byte reads as a three-byte replacement character: the limit holds for what the prompt carries.
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo/AGENTS.md").write_bytes(b"\xff" * 65_536)

    (file,) = [found for found in load_instruction_files(str(tmp_path / "repo"), tmp_path / "none") if
               Path(found.path).is_relative_to(tmp_path)]
    section = project_context([file])
    assert len(section.encode("utf-8")) <= 65_536
    ((_, size),) = _NOTICE.findall(section)
    assert size == str(3 * 65_536)
