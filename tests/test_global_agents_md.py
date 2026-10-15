"""Unit tests for vibe.global_agents_md — per-backend global instruction file
resolution + the read/write/sync helpers that back the Web UI "Global prompts"
dialog. All tests pass an isolated ``home`` so they never touch the real
``~/.claude`` / ``~/.codex`` / ``~/.config/opencode`` files; Vibey's file follows the
Avibe home, which each test moves under its own ``tmp_path``."""

import os

import pytest

from pathlib import Path

from vibe.global_agents_md import (
    global_instruction_path,
    read_all_global_agents_md,
    read_global_agents_md,
    write_global_agents_md,
    write_many_global_agents_md,
)


@pytest.fixture(autouse=True)
def _avibe_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".avibe"))


# --- path resolution ------------------------------------------------------


def test_path_resolution_per_backend(tmp_path: Path):
    # Vibey's is the one it loads every Turn (C-7 tools.md section 9), in the Avibe home.
    assert global_instruction_path("vibey", tmp_path) == tmp_path / ".avibe" / "AGENTS.md"
    assert global_instruction_path("claude", tmp_path) == tmp_path / ".claude" / "CLAUDE.md"
    assert global_instruction_path("codex", tmp_path) == tmp_path / ".codex" / "AGENTS.md"
    assert (
        global_instruction_path("opencode", tmp_path)
        == tmp_path / ".config" / "opencode" / "AGENTS.md"
    )


def test_path_resolution_unknown_backend_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        global_instruction_path("cursor", tmp_path)


def test_opencode_ignores_legacy_config_dir(tmp_path: Path):
    # OpenCode reads global rules from ~/.config/opencode/AGENTS.md regardless of
    # a legacy ~/.opencode config dir — editing the latter would have no effect.
    alt = tmp_path / ".opencode"
    alt.mkdir()
    (alt / "opencode.json").write_text("{}", encoding="utf-8")
    (alt / "AGENTS.md").write_text("legacy", encoding="utf-8")
    assert (
        global_instruction_path("opencode", tmp_path)
        == tmp_path / ".config" / "opencode" / "AGENTS.md"
    )


# --- read -----------------------------------------------------------------


def test_read_missing_file_is_empty(tmp_path: Path):
    result = read_global_agents_md("claude", tmp_path)
    assert result == {
        "backend": "claude",
        "path": str(tmp_path / ".claude" / "CLAUDE.md"),
        "filename": "CLAUDE.md",
        "content": "",
        "exists": False,
        "read_error": False,
    }


def test_read_invalid_utf8_sets_read_error(tmp_path: Path):
    # A non-UTF-8 file must degrade gracefully (read_error flag) rather than
    # raising — otherwise one bad file would 500 the whole editor.
    path = tmp_path / ".codex" / "AGENTS.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe garbage")
    result = read_global_agents_md("codex", tmp_path)
    assert result["read_error"] is True
    assert result["exists"] is True
    assert result["content"] == ""


def test_read_existing_file(tmp_path: Path):
    path = tmp_path / ".codex" / "AGENTS.md"
    path.parent.mkdir(parents=True)
    path.write_text("be concise", encoding="utf-8")
    result = read_global_agents_md("codex", tmp_path)
    assert result["content"] == "be concise"
    assert result["exists"] is True
    assert result["filename"] == "AGENTS.md"


def test_read_all_covers_every_backend(tmp_path: Path):
    results = read_all_global_agents_md(tmp_path)
    # Catalog order, the built-in backend first.
    assert [r["backend"] for r in results] == ["vibey", "opencode", "claude", "codex"]


# --- write ----------------------------------------------------------------


def test_write_creates_file_and_parents(tmp_path: Path):
    result = write_global_agents_md("claude", "hello world", tmp_path)
    path = tmp_path / ".claude" / "CLAUDE.md"
    assert path.read_text(encoding="utf-8") == "hello world"
    assert result["exists"] is True
    assert result["content"] == "hello world"


def test_write_overwrites_existing(tmp_path: Path):
    write_global_agents_md("opencode", "v1", tmp_path)
    write_global_agents_md("opencode", "v2", tmp_path)
    assert (tmp_path / ".config" / "opencode" / "AGENTS.md").read_text(encoding="utf-8") == "v2"


def test_write_preserves_symlink(tmp_path: Path):
    # A symlinked global prompt file (e.g. dotfiles repo) must stay a symlink;
    # the write updates the link target rather than replacing the link.
    real = tmp_path / "dotfiles" / "CLAUDE.md"
    real.parent.mkdir(parents=True)
    real.write_text("old", encoding="utf-8")
    link = tmp_path / ".claude" / "CLAUDE.md"
    link.parent.mkdir(parents=True)
    os.symlink(real, link)
    write_global_agents_md("claude", "new", tmp_path)
    assert link.is_symlink()  # link preserved, not clobbered into a regular file
    assert real.read_text(encoding="utf-8") == "new"  # target updated through the link
    assert link.read_text(encoding="utf-8") == "new"


# --- write_many (per-backend Save + one-click Sync) -----------------------


def test_write_many_single_backend_only_writes_that_one(tmp_path: Path):
    write_many_global_agents_md(["claude"], "only claude", tmp_path)
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == "only claude"
    assert not (tmp_path / ".codex" / "AGENTS.md").exists()
    assert not (tmp_path / ".config" / "opencode" / "AGENTS.md").exists()


def test_write_many_all_backends_syncs_identical_content(tmp_path: Path):
    result = write_many_global_agents_md(["vibey", "claude", "codex", "opencode"], "shared", tmp_path)
    assert (tmp_path / ".avibe" / "AGENTS.md").read_text(encoding="utf-8") == "shared"
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == "shared"
    assert (tmp_path / ".codex" / "AGENTS.md").read_text(encoding="utf-8") == "shared"
    assert (tmp_path / ".config" / "opencode" / "AGENTS.md").read_text(encoding="utf-8") == "shared"
    # The refreshed seed reflects all four as existing with the synced content.
    assert all(entry["exists"] and entry["content"] == "shared" for entry in result)


def test_write_many_unknown_backend_raises_before_writing(tmp_path: Path):
    # Validation happens up front, so a bad id cannot half-apply the sync.
    with pytest.raises(ValueError):
        write_many_global_agents_md(["claude", "bogus"], "should not persist", tmp_path)
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


def test_write_many_empty_list_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        write_many_global_agents_md([], "x", tmp_path)
