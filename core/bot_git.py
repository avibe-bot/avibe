"""Automatic Git history for Bot directories (``docs/plans/bots.md`` D7).

A Bot's directory is an ordinary repository with ``.git`` in place. Avibe commits before each Turn of the Bot's
Session (what others changed since, attributed to them) and after it (the Bot's own changes, with the input's summary
as the subject). The Bot is not taught to commit; it reads the history to trace or undo. History is never squashed
or pruned: only ``git gc --auto`` runs, when Git's own thresholds say the repository has grown.

The commits run on the Show Page checkpoint worker (``core.show_git.ShowGitCheckpointService``), off the controller
loop, and the Turn's dispatch waits for its PRE_TURN commit before the prompt reaches the backend.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from core.git_binary import ResolvedGit

logger = logging.getLogger(__name__)

PRE_TURN = "pre-turn"
POST_TURN = "post-turn"
GIT_COMMAND_TIMEOUT_SECONDS = 60
GIT_MAINTENANCE_TIMEOUT_SECONDS = 5 * 60
STALE_INDEX_LOCK_SECONDS = 5 * 60
OTHERS_SUBJECT = "Changes by others"
TURN_SUBJECT_FALLBACK = "Turn"
_OTHERS_AUTHOR = ("Others", "others@avibe.local")
_COMMITTER = ("Avibe", "checkpoint@avibe.local")
_HOOKS_DIRNAME = "avibe-empty-hooks"


class BotGitError(RuntimeError):
    """A Git operation on a Bot directory failed."""


def bot_git_environment() -> dict[str, str]:
    """The service's environment without Git overrides or the user's Git configuration (no hooks, no signing)."""
    from core.show_git import _git_environment

    return _git_environment()


class BotGitRepository:
    def __init__(self, handle: str, directory: Path, git: ResolvedGit) -> None:
        self.handle = handle
        self.directory = directory
        self.git = git

    @property
    def gitdir(self) -> Path:
        return self.directory / ".git"

    def checkpoint(
        self,
        checkpoint: str,
        *,
        session_id: str,
        message: str = "",
        run_id: Optional[str] = None,
    ) -> bool:
        """Commit everything the directory holds now, if anything changed. Returns whether a commit was made."""
        from core.show_git import _checkpoint_message, sanitize_checkpoint_subject

        if checkpoint not in {PRE_TURN, POST_TURN}:
            raise ValueError(f"unsupported checkpoint kind: {checkpoint}")
        if not self.directory.is_dir():
            return False
        if not self.gitdir.exists():
            self._run_checked(["-c", "init.defaultBranch=main", "init", "--quiet", str(self.directory)], bare=True)
        if not self.gitdir.is_dir():
            # A ``.git`` file points elsewhere: the user moved the history on purpose, and it is theirs to keep.
            return False
        self._remove_stale_index_lock()
        self._ensure_empty_hooks_dir()
        status = self._run_checked(["status", "--porcelain=v1", "--untracked-files=all"])
        if not status.stdout.strip():
            return False
        if checkpoint == PRE_TURN:
            subject, author = OTHERS_SUBJECT, _OTHERS_AUTHOR
        else:
            subject = sanitize_checkpoint_subject(message) if str(message or "").strip() else TURN_SUBJECT_FALLBACK
            author = (f"@{self.handle}", f"{self.handle}@bots.avibe.local")
        self._run_checked(["add", "-A"])
        result = self._run(
            [
                "commit",
                "--quiet",
                f"--author={author[0]} <{author[1]}>",
                "-m",
                _checkpoint_message(subject, session_id=session_id, run_id=run_id, checkpoint=checkpoint),
            ]
        )
        if result.returncode != 0:
            if "nothing to commit" in (result.stdout + result.stderr).lower():
                return False
            raise BotGitError(result.stderr.strip() or result.stdout.strip() or "failed to commit the Bot directory")
        if checkpoint == POST_TURN:
            self._maintain()
        return True

    def _maintain(self) -> None:
        """``git gc --auto``: packs when Git's thresholds say so. Never squashes, never drops a commit."""
        result = self._run(["-c", "gc.autoDetach=false", "gc", "--auto", "--quiet"], timeout=GIT_MAINTENANCE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            logger.warning("git gc --auto failed for the Bot directory %s: %s", self.directory, result.stderr.strip())

    def _remove_stale_index_lock(self) -> None:
        lock = self.gitdir / "index.lock"
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return
        if age > STALE_INDEX_LOCK_SECONDS:
            try:
                lock.unlink()
            except OSError:
                logger.warning("failed to remove a stale Bot directory index lock: %s", lock, exc_info=True)

    def _ensure_empty_hooks_dir(self) -> None:
        hooks = self.gitdir / _HOOKS_DIRNAME
        if hooks.is_symlink() or (hooks.exists() and not hooks.is_dir()):
            hooks.unlink()
        hooks.mkdir(exist_ok=True)
        for child in hooks.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _base_command(self) -> list[str]:
        return [
            str(self.git.path),
            "-c", f"user.name={_COMMITTER[0]}",
            "-c", f"user.email={_COMMITTER[1]}",
            "-c", "commit.gpgsign=false",
            "-c", f"core.hooksPath={self.gitdir / _HOOKS_DIRNAME}",
            # Commits never start background maintenance; ``_maintain`` runs it in the foreground instead.
            "-c", "maintenance.auto=false",
        ]

    def _run(self, args: list[str], *, timeout: int = GIT_COMMAND_TIMEOUT_SECONDS, bare: bool = False):
        location = [] if bare else ["-C", str(self.directory)]
        try:
            return subprocess.run(
                [*self._base_command(), *location, *args],
                check=False,
                capture_output=True,
                text=True,
                env=bot_git_environment(),
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BotGitError(f"failed to execute a Bot directory Git command: {exc}") from exc

    def _run_checked(self, args: list[str], *, bare: bool = False):
        result = self._run(args, bare=bare)
        if result.returncode != 0:
            raise BotGitError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
        return result
