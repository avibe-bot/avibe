"""A Bot directory's automatic Git history (``docs/plans/bots.md`` D7)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from config import paths
from core import show_git
from core.bot_git import POST_TURN, PRE_TURN, BotGitRepository
from core.bots import ensure_bot_directory
from core.git_binary import ResolvedGit
from core.inbox_events import InboxEventBus
from core.show_git import ShowGitCheckpointService, TurnCheckpointContext


@pytest.fixture
def git(monkeypatch, tmp_path) -> ResolvedGit:
    candidate = shutil.which("git")
    if candidate is None:
        pytest.skip("system Git is required for Bot directory history")
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    return ResolvedGit(path=Path(candidate), source="system")


def _log(directory: Path, fmt: str = "%an|%s") -> list[str]:
    return subprocess.run(
        ["git", "-C", str(directory), "log", f"--format={fmt}"], check=True, capture_output=True, text=True
    ).stdout.splitlines()


def test_turn_checkpoints_attribute_others_before_and_the_bot_after(git):
    directory = ensure_bot_directory("cso")
    repo = BotGitRepository("cso", directory, git)
    assert not repo.checkpoint(PRE_TURN, session_id="ses_cso")  # git init and nothing else: nothing to record

    (directory / "AGENTS.md").write_text("# Notes from the user\n")
    assert repo.checkpoint(PRE_TURN, session_id="ses_cso", run_id="run-1")
    (directory / "AGENTS.md").write_text("# Notes\n- Prefer short reports.\n")
    assert repo.checkpoint(POST_TURN, session_id="ses_cso", message="Remember: short reports\nplease", run_id="run-1")
    assert not repo.checkpoint(POST_TURN, session_id="ses_cso", message="nothing changed")

    assert _log(directory) == ["@cso|Remember: short reports please", "Others|Changes by others"]
    body = _log(directory, "%B")
    assert "Avibe-Session: ses_cso" in body and "Avibe-Checkpoint: post-turn" in body


def test_history_is_never_squashed(git, monkeypatch):
    # The Show Page history squashes past its limits; a Bot's memory history keeps every commit.
    monkeypatch.setattr(show_git, "MAX_COMMITS", 1)
    directory = ensure_bot_directory("main")
    repo = BotGitRepository("main", directory, git)
    for index in range(4):
        (directory / "notes.md").write_text(f"{index}\n")
        repo.checkpoint(POST_TURN, session_id="ses_main", message=f"turn {index}")
    assert len(_log(directory)) == 4


def test_a_bot_sessions_turn_boundaries_commit_its_directory(git, monkeypatch):
    directory = ensure_bot_directory("cso")
    (directory / "draft.md").write_text("edited by the user between turns\n")
    monkeypatch.setattr(
        show_git,
        "load_turn_checkpoint_context",
        lambda _session_id, **_kwargs: TurnCheckpointContext(message="Review the login flow", message_id="m1"),
    )
    service = ShowGitCheckpointService(git)
    bus = InboxEventBus()
    service.start(bus)
    try:
        bus.publish("turn.start", {"session_id": "ses_cso", "turn_id": "t1"})
        # The dispatch waits for the PRE_TURN commit, so it precedes the Bot's first edit.
        asyncio.run(service.wait_for_turn_start("ses_cso"))
        assert _log(directory) == ["Others|Changes by others"]
        (directory / "AGENTS.md").write_text("# Security notes\n")
        bus.publish("turn.end", {"session_id": "ses_cso", "turn_id": "t1"})
    finally:
        service.stop()
    assert _log(directory) == ["@cso|Review the login flow", "Others|Changes by others"]
    # A Bot Session is not a Show Page: no shadow repository appears for it.
    assert not paths.get_show_git_dir("ses_cso").exists()
