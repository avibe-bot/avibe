from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import paths
from core import show_git
from core import inbox_events
from core.git_binary import ResolvedGit
from core.inbox_events import InboxEventBus
from core.session_turns import SessionTurnManager
from core.show_git import (
    POST_TURN,
    PRE_TURN,
    ShowGitCheckpointService,
    ShowGitRepository,
    TurnCheckpointContext,
    show_history_status,
    sanitize_checkpoint_subject,
)


@pytest.fixture
def resolved_git() -> ResolvedGit:
    candidate = shutil.which("git")
    if candidate is None:
        pytest.skip("system Git is required for Show checkpoint tests")
    return ResolvedGit(path=Path(candidate), source="system")


def _workspace(session_id: str = "ses_show_git") -> Path:
    workspace = paths.get_show_page_dir(session_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _repo(session_id: str, resolved_git: ResolvedGit) -> ShowGitRepository:
    return ShowGitRepository(session_id, resolved_git)


def _platform_git(repo: ShowGitRepository, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(key, None)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    return subprocess.run(
        [
            str(repo.git.path),
            f"--git-dir={repo.gitdir}",
            f"--work-tree={repo.workspace}",
            *args,
        ],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _native_git(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    return subprocess.run(
        [shutil.which("git") or "git", "-C", str(workspace), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _commit_count(repo: ShowGitRepository) -> int:
    return int(_platform_git(repo, "rev-list", "--count", "refs/heads/main").stdout.strip())


def _head_message(repo: ShowGitRepository) -> str:
    return _platform_git(repo, "log", "-1", "--format=%B").stdout.rstrip()


def test_missing_workspace_never_creates_checkpoint_paths(resolved_git):
    repo = _repo("ses_missing", resolved_git)

    assert repo.checkpoint(PRE_TURN) is False
    assert not repo.workspace.exists()
    assert not repo.gitdir.exists()
    assert not repo.gitdir.parent.exists()


def test_history_status_never_creates_workspace_or_repository():
    session_id = "ses_guidance_missing"
    workspace = paths.get_show_page_dir(session_id)
    gitdir = paths.get_show_git_dir(session_id)

    status = show_history_status(session_id)

    assert status["mode"] == "managed"
    assert status["git_dir"] == str(gitdir)
    assert not workspace.exists()
    assert not gitdir.exists()
    assert not gitdir.parent.exists()


@pytest.mark.parametrize("session_id", ["", "../other", "ses/name", "ses\\name"])
def test_history_status_rejects_invalid_session_ids(session_id):
    with pytest.raises(ValueError, match="Invalid Show Page session id"):
        show_history_status(session_id)


def test_lazy_adoption_is_idempotent_and_native_git_works(resolved_git):
    session_id = "ses_adopt"
    workspace = _workspace(session_id)
    (workspace / "src").mkdir()
    (workspace / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)

    assert repo.checkpoint(PRE_TURN, run_id="run-adopt") is True
    pointer = workspace / ".git"
    original_pointer = pointer.read_text(encoding="utf-8")
    original_pointer_mtime = pointer.stat().st_mtime_ns

    assert repo.checkpoint(PRE_TURN, run_id="run-adopt") is False
    assert pointer.read_text(encoding="utf-8") == original_pointer
    assert pointer.stat().st_mtime_ns == original_pointer_mtime
    assert _commit_count(repo) == 1
    assert _native_git(workspace, "status", "--porcelain").stdout == ""
    assert _native_git(workspace, "log", "-1", "--format=%s").stdout.strip() == "adopt existing workspace"
    assert "Avibe-Run: run-adopt" in _head_message(repo)
    assert "Avibe-Checkpoint: adopt" in _head_message(repo)
    exclude = (repo.gitdir / "info" / "exclude").read_text(encoding="utf-8")
    assert all(pattern in exclude.splitlines() for pattern in ("node_modules/", "dist/", ".vite/"))


def test_lazy_adoption_does_not_require_initial_branch_option(resolved_git, monkeypatch):
    session_id = "ses_adopt_old_git"
    workspace = _workspace(session_id)
    (workspace / "page.txt").write_text("content\n", encoding="utf-8")
    commands = []
    real_run = subprocess.run

    def reject_new_init_option(command, **kwargs):
        commands.append(command)
        if any(str(part).startswith("--initial-branch") for part in command):
            return subprocess.CompletedProcess(command, 129, "", "unknown option: initial-branch")
        return real_run(command, **kwargs)

    monkeypatch.setattr(show_git.subprocess, "run", reject_new_init_option)
    repo = _repo(session_id, resolved_git)

    assert repo.checkpoint(PRE_TURN) is True
    assert _platform_git(repo, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    init_command = next(command for command in commands if "init" in command)
    assert "--bare" in init_command
    assert not any(str(part).startswith("--initial-branch") for part in init_command)


def test_user_git_directory_is_untouched_while_shadow_checkpoints_continue(resolved_git):
    session_id = "ses_shadow"
    workspace = _workspace(session_id)
    (workspace / "app.txt").write_text("user v1\n", encoding="utf-8")
    _native_git(workspace, "init", "--initial-branch=trunk")
    _native_git(workspace, "add", "app.txt")
    _native_git(
        workspace,
        "-c",
        "user.name=user-owner",
        "-c",
        "user.email=user@example.test",
        "commit",
        "-m",
        "user baseline",
    )
    user_git = workspace / ".git"
    before = {
        "HEAD": (user_git / "HEAD").read_bytes(),
        "config": (user_git / "config").read_bytes(),
        "index": (user_git / "index").read_bytes(),
        "ref": (user_git / "refs" / "heads" / "trunk").read_bytes(),
    }
    repo = _repo(session_id, resolved_git)

    assert repo.checkpoint(PRE_TURN) is True
    (workspace / "app.txt").write_text("user v2\n", encoding="utf-8")
    assert repo.checkpoint(POST_TURN, message="shadow update") is True

    assert user_git.is_dir()
    assert before == {
        "HEAD": (user_git / "HEAD").read_bytes(),
        "config": (user_git / "config").read_bytes(),
        "index": (user_git / "index").read_bytes(),
        "ref": (user_git / "refs" / "heads" / "trunk").read_bytes(),
    }
    assert _native_git(workspace, "status", "--porcelain").stdout.strip() == "M app.txt"
    assert _commit_count(repo) == 2

    shutil.rmtree(user_git)
    (workspace / "app.txt").write_text("user v3\n", encoding="utf-8")
    assert repo.checkpoint(POST_TURN, message="return to managed") is True
    assert (workspace / ".git").is_file()
    assert _native_git(workspace, "log", "-1", "--format=%s").stdout.strip() == "return to managed"


def test_user_git_pointer_is_never_overwritten(resolved_git, tmp_path):
    session_id = "ses_user_pointer"
    workspace = _workspace(session_id)
    (workspace / "app.txt").write_text("content\n", encoding="utf-8")
    user_gitdir = tmp_path / "owner-repository.git"
    subprocess.run(
        [str(resolved_git.path), "init", "--bare", str(user_gitdir)],
        check=True,
        capture_output=True,
        text=True,
    )
    pointer = workspace / ".git"
    pointer.write_text(f"gitdir: {user_gitdir}\n", encoding="utf-8")
    before = pointer.read_bytes()

    assert _repo(session_id, resolved_git).checkpoint(PRE_TURN) is True
    assert pointer.read_bytes() == before


def test_existing_user_git_pointer_with_managed_path_shape_is_never_overwritten(resolved_git, tmp_path):
    session_id = "ses_user_shaped_pointer"
    workspace = _workspace(session_id)
    (workspace / "app.txt").write_text("content\n", encoding="utf-8")
    user_gitdir = tmp_path / "show-git" / f"{session_id}.git"
    user_gitdir.parent.mkdir()
    subprocess.run(
        [str(resolved_git.path), "init", "--bare", str(user_gitdir)],
        check=True,
        capture_output=True,
        text=True,
    )
    pointer = workspace / ".git"
    pointer.write_text(f"gitdir: {user_gitdir}\n", encoding="utf-8")
    before = pointer.read_bytes()

    repo = _repo(session_id, resolved_git)
    assert repo.checkpoint(PRE_TURN) is True

    assert pointer.read_bytes() == before
    assert repo.gitdir != user_gitdir
    assert _commit_count(repo) == 1
    status = show_history_status(session_id)
    assert status["mode"] == "self-managed"
    assert status["git_dir"] == str(repo.gitdir)
    assert pointer.read_bytes() == before


def test_pre_and_post_turn_commit_only_when_dirty_with_frozen_messages(resolved_git):
    session_id = "ses_turns"
    workspace = _workspace(session_id)
    target = workspace / "page.txt"
    target.write_text("v1\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)
    repo.checkpoint(PRE_TURN)

    target.write_text("manual\n", encoding="utf-8")
    assert repo.checkpoint(PRE_TURN, run_id="run-pre") is True
    assert _head_message(repo).startswith("out-of-band changes\n\n")
    assert "Avibe-Run: run-pre" in _head_message(repo)
    assert "Avibe-Checkpoint: pre-turn" in _head_message(repo)
    count = _commit_count(repo)
    assert repo.checkpoint(POST_TURN, message="unchanged") is False
    assert _commit_count(repo) == count

    driving_message = "  Build\n\tthe\x1b   new page " + ("x" * 100)
    target.write_text("agent\n", encoding="utf-8")
    assert repo.checkpoint(POST_TURN, message=driving_message, run_id="run-post") is True
    assert _platform_git(repo, "log", "-1", "--format=%s").stdout.strip() == sanitize_checkpoint_subject(
        driving_message
    )
    assert "Avibe-Run: run-post" in _head_message(repo)
    assert "Avibe-Checkpoint: post-turn" in _head_message(repo)

    target.write_text("fallback\n", encoding="utf-8")
    assert repo.checkpoint(POST_TURN, message="\n\t") is True
    assert _platform_git(repo, "log", "-1", "--format=%s").stdout.strip() == "checkpoint"


def test_native_restore_is_recorded_as_a_forward_post_turn_commit(resolved_git):
    session_id = "ses_restore"
    workspace = _workspace(session_id)
    target = workspace / "page.txt"
    target.write_text("v1\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)
    repo.checkpoint(PRE_TURN)
    target.write_text("v2\n", encoding="utf-8")
    repo.checkpoint(POST_TURN, message="write v2")
    v2_head = _platform_git(repo, "rev-parse", "HEAD").stdout.strip()

    _native_git(workspace, "restore", "--source=HEAD~1", "--", "page.txt")
    assert repo.checkpoint(POST_TURN, message="restore v1") is True

    assert target.read_text(encoding="utf-8") == "v1\n"
    assert _platform_git(repo, "rev-parse", "HEAD^").stdout.strip() == v2_head
    assert _platform_git(repo, "log", "-1", "--format=%s").stdout.strip() == "restore v1"


@pytest.mark.parametrize("self_managed", [False, True])
def test_skill_status_drives_native_restore_without_changing_user_history(resolved_git, self_managed):
    session_id = "ses_skill_restore"
    workspace = _workspace(session_id)
    target = workspace / "page.txt"
    target.write_text("first\n")
    if self_managed:
        _native_git(workspace, "init", "--initial-branch=trunk")
        _native_git(workspace, "add", "page.txt")
        _native_git(workspace, "-c", "user.name=fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "baseline")
    user_git = workspace / ".git"
    before = {str(path.relative_to(user_git)): path.read_bytes() for path in user_git.rglob("*") if path.is_file()} if self_managed else None
    repo = _repo(session_id, resolved_git)
    assert repo.checkpoint(PRE_TURN)
    target.write_text("second\n")
    assert repo.checkpoint(POST_TURN, message="second")
    head = _platform_git(repo, "rev-parse", "HEAD").stdout.strip()

    status = show_history_status(session_id)
    assert status["mode"] == ("self-managed" if self_managed else "managed")
    assert Path(status["git_dir"]).resolve() == repo.gitdir.resolve()
    if self_managed:
        _platform_git(repo, "log", "-1")
        _platform_git(repo, "restore", "--source=HEAD~1", "--", "page.txt")
    else:
        actual_git = _native_git(workspace, "rev-parse", "--absolute-git-dir").stdout.strip()
        assert Path(actual_git).resolve() == Path(status["git_dir"]).resolve()
        _native_git(workspace, "log", "-1")
        _native_git(workspace, "restore", "--source=HEAD~1", "--", "page.txt")
    assert target.read_text() == "first\n"
    assert _platform_git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert repo.checkpoint(POST_TURN, message="restore first")
    assert _platform_git(repo, "rev-parse", "HEAD^").stdout.strip() == head
    if self_managed:
        assert {str(path.relative_to(user_git)): path.read_bytes() for path in user_git.rglob("*") if path.is_file()} == before


def test_main_rewind_is_converted_to_a_forward_commit(resolved_git):
    session_id = "ses_rewind"
    workspace = _workspace(session_id)
    target = workspace / "page.txt"
    target.write_text("v1\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)
    repo.checkpoint(PRE_TURN)
    target.write_text("v2\n", encoding="utf-8")
    repo.checkpoint(POST_TURN, message="write v2")
    v2_head = _platform_git(repo, "rev-parse", "HEAD").stdout.strip()

    _native_git(workspace, "reset", "--hard", "HEAD~1")
    assert _native_git(workspace, "rev-parse", "HEAD").stdout.strip() != v2_head
    assert repo.checkpoint(POST_TURN, message="recover after reset") is True

    assert target.read_text(encoding="utf-8") == "v1\n"
    assert _platform_git(repo, "rev-parse", "HEAD^").stdout.strip() == v2_head
    assert _platform_git(repo, "rev-parse", "refs/avibe/checkpoint-main").stdout.strip() == _platform_git(
        repo, "rev-parse", "HEAD"
    ).stdout.strip()


def test_detached_head_and_stale_index_lock_self_heal(resolved_git):
    session_id = "ses_heal"
    workspace = _workspace(session_id)
    target = workspace / "page.txt"
    target.write_text("v1\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)
    repo.checkpoint(PRE_TURN)
    _native_git(workspace, "checkout", "--detach", "HEAD")
    lock = repo.gitdir / "index.lock"
    lock.write_text("stale", encoding="utf-8")
    old = time.time() - show_git.STALE_INDEX_LOCK_SECONDS - 5
    os.utime(lock, (old, old))
    target.write_text("v2\n", encoding="utf-8")

    assert repo.checkpoint(POST_TURN, message="heal history") is True
    assert not lock.exists()
    assert _platform_git(repo, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    assert _platform_git(repo, "show", "HEAD:page.txt").stdout == "v2\n"


def test_dangling_managed_pointer_reinitializes_and_adopts(resolved_git, tmp_path):
    session_id = "ses_dangling"
    workspace = _workspace(session_id)
    (workspace / "page.txt").write_text("recovered\n", encoding="utf-8")
    stale = tmp_path / "old-home" / "show-git" / f"{session_id}.git"
    pointer = workspace / ".git"
    pointer.write_text(f"gitdir: {stale}\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)

    assert repo.checkpoint(PRE_TURN) is True
    assert pointer.read_text(encoding="utf-8") == f"gitdir: {repo.gitdir.resolve()}\n"
    assert repo.gitdir.is_dir()
    assert _platform_git(repo, "show", "HEAD:page.txt").stdout == "recovered\n"
    assert _platform_git(repo, "log", "-1", "--format=%s").stdout.strip() == "adopt existing workspace"


def test_prune_squashes_to_current_baseline_without_remote(resolved_git, monkeypatch):
    monkeypatch.setattr(show_git, "MAX_COMMITS", 2)
    session_id = "ses_prune"
    workspace = _workspace(session_id)
    target = workspace / "page.txt"
    target.write_text("v1\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)
    repo.checkpoint(PRE_TURN)

    target.write_text("v2\n", encoding="utf-8")
    repo.checkpoint(POST_TURN, message="second")
    target.write_text("v3\n", encoding="utf-8")
    repo.checkpoint(POST_TURN, message="third", run_id="run-prune")

    assert _commit_count(repo) == 1
    assert _platform_git(repo, "show", "HEAD:page.txt").stdout == "v3\n"
    assert _platform_git(repo, "log", "-1", "--format=%s").stdout.strip() == "third"
    assert "Avibe-Run: run-prune" in _head_message(repo)


def test_remote_freezes_history_rewrite_but_allows_gc(resolved_git, monkeypatch):
    monkeypatch.setattr(show_git, "MAX_COMMITS", 1)
    session_id = "ses_remote_freeze"
    workspace = _workspace(session_id)
    target = workspace / "page.txt"
    target.write_text("v1\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)
    repo.checkpoint(PRE_TURN)
    _platform_git(repo, "remote", "add", "backup", "https://example.invalid/show.git")

    target.write_text("v2\n", encoding="utf-8")
    assert repo.checkpoint(POST_TURN, message="preserve published ancestry") is True
    assert _commit_count(repo) == 2
    assert _platform_git(repo, "remote").stdout.strip() == "backup"


def test_gitdir_size_limit_triggers_baseline_even_without_new_changes(resolved_git, monkeypatch):
    session_id = "ses_size_prune"
    workspace = _workspace(session_id)
    (workspace / "page.txt").write_text("stable\n", encoding="utf-8")
    repo = _repo(session_id, resolved_git)
    repo.checkpoint(PRE_TURN)
    (workspace / "page.txt").write_text("stable v2\n", encoding="utf-8")
    repo.checkpoint(POST_TURN, message="second")
    old_head = _platform_git(repo, "rev-parse", "HEAD").stdout.strip()
    old_tree = _platform_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    monkeypatch.setattr(show_git, "MAX_GITDIR_BYTES", 1)

    assert repo.checkpoint(POST_TURN, message="unchanged") is False

    new_head = _platform_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert new_head != old_head
    assert _platform_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip() == old_tree
    assert _commit_count(repo) == 1


def test_platform_git_scrubs_environment_and_ignores_global_hooks(resolved_git, monkeypatch, tmp_path):
    session_id = "ses_isolated_git"
    workspace = _workspace(session_id)
    (workspace / "page.txt").write_text("isolated\n", encoding="utf-8")
    malicious_hooks = tmp_path / "hooks"
    malicious_hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = malicious_hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    malicious_config = tmp_path / "gitconfig"
    malicious_config.write_text(
        "[user]\n\tname = ambient-user\n\temail = ambient@example.test\n"
        f"[core]\n\thooksPath = {malicious_hooks}\n"
        "[commit]\n\tgpgsign = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "poison.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "poison-worktree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "poison-index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "poison-objects"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(malicious_config))
    captured = []
    real_run = subprocess.run

    def recording_run(command, **kwargs):
        env = dict(kwargs.get("env") or {})
        captured.append(
            (
                command,
                {
                    "GIT_CONFIG_GLOBAL": env.get("GIT_CONFIG_GLOBAL"),
                    "GIT_CONFIG_SYSTEM": env.get("GIT_CONFIG_SYSTEM"),
                    **{key: key in env for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")},
                },
            )
        )
        return real_run(command, **kwargs)

    monkeypatch.setattr(show_git.subprocess, "run", recording_run)
    repo = _repo(session_id, resolved_git)

    assert repo.checkpoint(PRE_TURN) is True
    platform_calls = list(captured)
    monkeypatch.setattr(show_git.subprocess, "run", real_run)
    assert not marker.exists()
    _platform_git(repo, "config", "core.hooksPath", str(malicious_hooks))
    (workspace / "page.txt").write_text("isolated v2\n", encoding="utf-8")
    assert repo.checkpoint(POST_TURN, message="local hooks stay disabled") is True
    assert not marker.exists()
    identity = _platform_git(repo, "log", "-1", "--format=%an|%ae").stdout.strip()
    assert identity == "avibe-checkpoint|checkpoint@avibe.local"
    assert platform_calls
    for command, env in platform_calls:
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_SYSTEM"] == os.devnull
        assert all(not env[key] for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"))
        assert "user.name=avibe-checkpoint" in command
        assert "user.email=checkpoint@avibe.local" in command
        assert "commit.gpgsign=false" in command
        assert "gc.auto=0" in command
        assert any(str(part).startswith("core.hooksPath=") for part in command)


def test_turn_event_subscriber_uses_storage_context_without_payload_changes(resolved_git, monkeypatch):
    session_id = "ses_bus"
    _workspace(session_id)
    calls = []

    class FakeRepository:
        def checkpoint(self, checkpoint, **kwargs):
            calls.append((checkpoint, kwargs))
            return True

    contexts = iter(
        (
            TurnCheckpointContext(message="", run_id="run-event"),
            TurnCheckpointContext(message="driving user message", run_id=None),
        )
    )
    lookups = []

    def load_context(loaded_session_id, **kwargs):
        lookups.append((loaded_session_id, kwargs))
        return next(contexts)

    monkeypatch.setattr(show_git, "load_turn_checkpoint_context", load_context)
    service = ShowGitCheckpointService(resolved_git)
    monkeypatch.setattr(service, "_repository", lambda _session_id: FakeRepository())
    bus = InboxEventBus()
    service.start(bus)

    event = {"session_id": session_id, "turn_id": "turn-current"}
    bus.publish("turn.start", event)
    bus.publish("turn.end", event)
    service.stop()
    bus.publish("turn.end", {"session_id": session_id})

    assert calls == [
        (PRE_TURN, {"run_id": "run-event"}),
        (POST_TURN, {"message": "driving user message", "run_id": "run-event"}),
    ]
    assert lookups == [
        (session_id, {"turn_id": "turn-current"}),
        (session_id, {"turn_id": "turn-current"}),
    ]


def test_turn_event_subscriber_keeps_start_message_when_next_pending_can_arrive(resolved_git, monkeypatch):
    session_id = "ses_bus_cached"
    _workspace(session_id)
    calls = []

    class FakeRepository:
        def checkpoint(self, checkpoint, **kwargs):
            calls.append((checkpoint, kwargs))
            return True

    lookups = []

    def load_context(_session_id, **kwargs):
        lookups.append(kwargs)
        if kwargs:
            raise AssertionError("turn.end must not select a later pending message")
        return TurnCheckpointContext(message="current driving message", message_id="msg-current")

    monkeypatch.setattr(show_git, "load_turn_checkpoint_context", load_context)
    service = ShowGitCheckpointService(resolved_git)
    monkeypatch.setattr(service, "_repository", lambda _session_id: FakeRepository())
    bus = InboxEventBus()
    service.start(bus)

    bus.publish("turn.start", {"session_id": session_id})
    bus.publish("turn.end", {"session_id": session_id})
    service.stop()

    assert lookups == [{}]
    assert calls == [
        (PRE_TURN, {"run_id": None}),
        (POST_TURN, {"message": "current driving message", "run_id": None}),
    ]


def test_duplicate_turn_events_do_not_create_duplicate_checkpoint_commits(resolved_git, monkeypatch):
    session_id = "ses_bus_duplicate"
    workspace = _workspace(session_id)
    (workspace / "page.txt").write_text("before\n", encoding="utf-8")
    monkeypatch.setattr(
        show_git,
        "load_turn_checkpoint_context",
        lambda _session_id, **_kwargs: TurnCheckpointContext(message="edit page"),
    )
    service = ShowGitCheckpointService(resolved_git)
    bus = InboxEventBus()
    service.start(bus)

    bus.publish("turn.start", {"session_id": session_id})
    bus.publish("turn.start", {"session_id": session_id})
    (workspace / "page.txt").write_text("after\n", encoding="utf-8")
    bus.publish("turn.end", {"session_id": session_id})
    bus.publish("turn.end", {"session_id": session_id})
    service.stop()

    repo = _repo(session_id, resolved_git)
    assert _platform_git(repo, "rev-list", "--count", "main").stdout.strip() == "2"
    assert _platform_git(repo, "log", "--format=%s").stdout.splitlines() == [
        "edit page",
        "adopt existing workspace",
    ]


def test_shared_turn_hooks_reuse_path_owned_bus_lifecycle(resolved_git, monkeypatch):
    session_id = "ses_path_owned"
    context = SimpleNamespace(platform="avibe", platform_specific={"agent_session_id": session_id})
    controller = SimpleNamespace(
        _session_id_from_context=lambda _context: session_id,
        _get_session_key=lambda _context: "avibe::ses_path_owned",
    )
    calls = []

    class FakeRepository:
        def checkpoint(self, checkpoint, **kwargs):
            calls.append((checkpoint, kwargs))
            return True

    monkeypatch.setattr(
        show_git,
        "load_turn_checkpoint_context",
        lambda _session_id, **_kwargs: TurnCheckpointContext(message="edit page", message_id="message-1"),
    )
    service = ShowGitCheckpointService(resolved_git)
    monkeypatch.setattr(service, "_repository", lambda _session_id: FakeRepository())
    bus = InboxEventBus()
    service.start(bus)
    lifecycle = []
    subscription_id = bus.subscribe_callback(
        lambda event_type, data: lifecycle.append((event_type, data))
        if event_type in {"turn.start", "turn.end"}
        else None
    )

    try:
        bus.publish("turn.start", {"session_id": session_id})
        service.begin_turn(controller, context)
        _workspace(session_id)
        service.end_turn(context)
        bus.publish("turn.end", {"session_id": session_id})
    finally:
        bus.unsubscribe(subscription_id)
        service.stop()

    assert lifecycle == [
        ("turn.start", {"session_id": session_id}),
        ("turn.end", {"session_id": session_id}),
    ]
    assert calls == [(POST_TURN, {"message": "edit page", "run_id": None})]


def test_agent_initiated_turn_reuses_fsm_bus_lifecycle(resolved_git, monkeypatch):
    session_id = "ses_agent_initiated"
    _workspace(session_id)
    calls = []

    class FakeRepository:
        def checkpoint(self, checkpoint, **kwargs):
            calls.append((checkpoint, kwargs))
            return True

    monkeypatch.setattr(
        show_git,
        "load_turn_checkpoint_context",
        lambda _session_id, **_kwargs: TurnCheckpointContext(message="background result", message_id="message-1"),
    )
    service = ShowGitCheckpointService(resolved_git)
    monkeypatch.setattr(service, "_repository", lambda _session_id: FakeRepository())
    bus = InboxEventBus()
    monkeypatch.setattr(inbox_events, "bus", bus)
    service.start(bus)
    lifecycle = []
    subscription_id = bus.subscribe_callback(
        lambda event_type, data: lifecycle.append((event_type, data))
        if event_type in {"turn.start", "turn.end"}
        else None
    )
    context = SimpleNamespace(
        platform="avibe",
        channel_id=session_id,
        platform_specific={"agent_session_id": session_id, "turn_token": "turn-agent"},
    )
    controller = SimpleNamespace(
        _session_id_from_context=lambda _context: session_id,
        _get_session_key=lambda _context: f"avibe::{session_id}",
        set_agent_status=lambda _session_id, _status: None,
        show_git_checkpoint_service=service,
        agent_service=SimpleNamespace(runtime_turn_started=lambda _context: True),
    )
    manager = SessionTurnManager(controller)
    controller.get_turn_sink = manager.get_turn_sink

    async def _flush_queue(_session_id: str) -> bool:
        return False

    manager.flush_queue = _flush_queue

    async def _exercise() -> None:
        manager.on_running(context)
        assert lifecycle == []
        assert manager.register_agent_initiated_turn(context) is True
        manager.on_terminal_result(context, is_error=False)
        manager.on_terminal_delivery_complete(context)
        sink = manager.get_turn_sink(f"avibe::{session_id}")
        assert sink is not None
        sink["done_event"].set()
        await manager.in_flight[session_id].task

    try:
        asyncio.run(_exercise())
    finally:
        bus.unsubscribe(subscription_id)
        service.stop()

    assert lifecycle == [
        ("turn.start", {"session_id": session_id, "turn_id": "turn-agent"}),
        ("turn.end", {"session_id": session_id, "turn_id": "turn-agent"}),
    ]
    assert calls == [
        (PRE_TURN, {"run_id": None}),
        (POST_TURN, {"message": "background result", "run_id": None}),
    ]


def test_history_status_uses_startup_latched_checkpoint_service_state(resolved_git, monkeypatch):
    bus = InboxEventBus()
    unavailable_service = ShowGitCheckpointService(None)
    unavailable_service.start(bus)
    monkeypatch.setattr(show_git, "resolve_git", lambda: resolved_git)

    unavailable = show_history_status("ses_latched_unavailable")
    unavailable_status = json.loads(paths.get_show_git_runtime_status_path().read_text(encoding="utf-8"))
    unavailable_service.stop()

    active_service = ShowGitCheckpointService(resolved_git)
    active_service.start(bus)
    monkeypatch.setattr(show_git, "resolve_git", lambda: None)
    available = show_history_status("ses_latched_available")
    active_status = json.loads(paths.get_show_git_runtime_status_path().read_text(encoding="utf-8"))
    active_service.stop()

    assert unavailable["checkpointing_active"] is False
    assert unavailable_status["active"] is False
    assert available["checkpointing_active"] is True
    assert active_status["active"] is True
    assert active_status["service_pid"] == os.getpid()


def test_cross_process_checkpoint_status_requires_current_service_owner(monkeypatch):
    status_path = paths.get_show_git_runtime_status_path()
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps(
            {
                "version": show_git._CHECKPOINT_STATUS_VERSION,
                "active": True,
                "service_pid": 1234,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(show_git, "_checkpoint_service_active", None)
    monkeypatch.setattr("vibe.runtime.resolve_service_owner_pid", lambda **_kwargs: 5678)

    assert show_git.show_git_checkpointing_active() is False

    monkeypatch.setattr("vibe.runtime.resolve_service_owner_pid", lambda **_kwargs: 1234)
    assert show_git.show_git_checkpointing_active() is True


def test_storage_lookup_uses_turn_boundary_instead_of_later_pending_message():
    from sqlalchemy import update

    from storage import messages_service
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, messages, session_turns
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(primary_platform="avibe")
    session_id = "ses_storage_boundary"
    with get_cached_sqlite_engine().begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_storage_boundary",
            now="2026-07-11T00:00:00+00:00",
        )
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor=session_id,
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at="2026-07-11T00:00:00+00:00",
                updated_at="2026-07-11T00:00:00+00:00",
                last_active_at="2026-07-11T00:00:00+00:00",
            )
        )
        prior = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="user",
            message_type="user",
            text="prior user message",
        )
        result = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="agent",
            message_type="result",
            text="prior result",
        )
        conn.execute(update(messages).where(messages.c.id == prior["id"]).values(created_at="2098-01-01T00:00:00+00:00"))
        conn.execute(update(messages).where(messages.c.id == result["id"]).values(created_at="2098-01-01T00:00:01+00:00"))

    assert show_git.load_turn_checkpoint_context(session_id) == TurnCheckpointContext()

    with get_cached_sqlite_engine().begin() as conn:
        from storage import message_deliveries

        current = message_deliveries.insert_delivery(
            conn,
            delivery_id=message_deliveries.new_delivery_id(),
            session_id=session_id,
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id=session_id,
                platform="avibe",
                author="user",
                source="user",
                text="current driving message",
            ),
            dispatch_text="current driving message",
            now="2099-01-01T00:00:01+00:00",
        )
        message_deliveries.claim_start_batch(
            conn,
            turn_id="turn_storage_boundary",
            session_id=session_id,
            backend="codex",
            deliveries=[current],
            dispatch_text="current driving message",
        )
        conn.execute(
            update(session_turns)
            .where(session_turns.c.id == "turn_storage_boundary")
            .values(
                created_at="2099-01-01T00:00:01+00:00",
                started_at="2099-01-01T00:00:01+00:00",
            )
        )
        later = message_deliveries.insert_delivery(
            conn,
            delivery_id=message_deliveries.new_delivery_id(),
            session_id=session_id,
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id=session_id,
                platform="avibe",
                author="user",
                source="user",
                text="later pending message",
            ),
            dispatch_text="later pending message",
            now="2099-01-01T00:00:02+00:00",
        )
        assert later["message_id"] is None

    current_context = show_git.load_turn_checkpoint_context(session_id)
    assert current_context.message == "current driving message"
    assert current_context.message_id == current["id"]
    context = show_git.load_turn_checkpoint_context(session_id, after="2099-01-01T00:00:00+00:00")
    assert context.message == "current driving message"
    assert context.message_id == current["id"]
    exact_context = show_git.load_turn_checkpoint_context(
        session_id,
        turn_id="turn_storage_boundary",
        after="2100-01-01T00:00:00+00:00",
    )
    assert exact_context.message == "current driving message"
    assert exact_context.turn_id == "turn_storage_boundary"


def test_storage_lookup_includes_harness_driving_message():
    from storage import messages_service
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, messages
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(primary_platform="avibe")
    session_id = "ses_harness_checkpoint"
    with get_cached_sqlite_engine().begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_harness_checkpoint",
            now="2026-07-15T00:00:00+00:00",
        )
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor=session_id,
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at="2026-07-15T00:00:00+00:00",
                updated_at="2026-07-15T00:00:00+00:00",
                last_active_at="2026-07-15T00:00:00+00:00",
            )
        )
        prompt = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="harness",
            message_type=messages_service.HARNESS_TYPE,
            source="harness",
            text="refresh the dashboard",
            native_message_id="agent_run:run-harness",
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == prompt["id"])
            .values(created_at="2099-01-01T00:00:01+00:00")
        )

    expected = TurnCheckpointContext(
        message="refresh the dashboard",
        run_id="run-harness",
        message_id=prompt["id"],
    )
    assert show_git.load_turn_checkpoint_context(session_id) == expected
    assert show_git.load_turn_checkpoint_context(
        session_id, after="2099-01-01T00:00:00+00:00"
    ) == expected

    with get_cached_sqlite_engine().begin() as conn:
        for index, author in enumerate(("user", "agent"), start=2):
            display_only = messages_service.append(
                conn,
                scope_id=scope_id,
                session_id=session_id,
                platform="avibe",
                author=author,
                message_type=messages_service.ANNOTATION_TYPE,
                text=f"display-only annotation from {author}",
            )
            conn.execute(
                messages.update()
                .where(messages.c.id == display_only["id"])
                .values(created_at=f"2099-01-01T00:00:0{index}+00:00")
            )

    assert show_git.load_turn_checkpoint_context(session_id) == TurnCheckpointContext()
    assert show_git.load_turn_checkpoint_context(
        session_id, after="2099-01-01T00:00:01.500000+00:00"
    ) == TurnCheckpointContext()


def test_storage_lookup_uses_transcript_acceptance_order():
    from storage import messages_service
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, messages
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(primary_platform="avibe")
    session_id = "ses_acceptance_checkpoint"
    with get_cached_sqlite_engine().begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_acceptance_checkpoint",
            now="2026-08-04T00:00:00.000000+00:00",
        )
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor=session_id,
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at="2026-08-04T00:00:00.000000+00:00",
                updated_at="2026-08-04T00:00:00.000000+00:00",
                last_active_at="2026-08-04T00:00:00.000000+00:00",
            )
        )
        queued = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="user",
            message_type="user",
            source="user",
            text="queued input accepted last",
        )
        direct = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="user",
            message_type="user",
            source="user",
            text="direct input accepted first",
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == queued["id"])
            .values(
                created_at="2026-08-04T00:00:01.000000+00:00",
                delivered_at="2026-08-04T00:00:04.000000+00:00",
            )
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == direct["id"])
            .values(created_at="2026-08-04T00:00:02.000000+00:00")
        )

    expected = TurnCheckpointContext(
        message="queued input accepted last",
        message_id=queued["id"],
    )
    assert show_git.load_turn_checkpoint_context(session_id) == expected
    assert show_git.load_turn_checkpoint_context(
        session_id,
        after="2026-08-04T00:00:03.000000+00:00",
    ) == expected


def test_storage_lookup_includes_annotation_driving_message():
    from storage import messages_service
    from storage.db import get_cached_sqlite_engine
    from storage.importer import ensure_sqlite_state
    from storage.models import agent_sessions, messages
    from storage.settings_service import upsert_scope

    ensure_sqlite_state(primary_platform="avibe")
    session_id = "ses_annotation_checkpoint"
    with get_cached_sqlite_engine().begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_annotation_checkpoint",
            now="2026-07-27T00:00:00+00:00",
        )
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor=session_id,
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at="2026-07-27T00:00:00+00:00",
                updated_at="2026-07-27T00:00:00+00:00",
                last_active_at="2026-07-27T00:00:00+00:00",
            )
        )
        prompt = messages_service.append(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="harness",
            message_type=messages_service.ANNOTATION_TYPE,
            source="harness",
            text="tighten the heading hierarchy",
            native_message_id="show:show_evt_checkpoint",
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == prompt["id"])
            .values(created_at="2099-01-01T00:00:01+00:00")
        )

    expected = TurnCheckpointContext(
        message="tighten the heading hierarchy",
        message_id=prompt["id"],
    )
    assert show_git.load_turn_checkpoint_context(session_id) == expected
    assert show_git.load_turn_checkpoint_context(
        session_id, after="2099-01-01T00:00:00+00:00"
    ) == expected
