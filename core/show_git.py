"""Native Git checkpoints for existing Show Page workspaces."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import paths
from core.git_binary import ResolvedGit, resolve_git

logger = logging.getLogger(__name__)

PRE_TURN = "pre-turn"
POST_TURN = "post-turn"
ADOPT = "adopt"
MAX_COMMITS = 500
MAX_GITDIR_BYTES = 200 * 1024 * 1024
STALE_INDEX_LOCK_SECONDS = 5 * 60
GIT_COMMAND_TIMEOUT_SECONDS = 60
GIT_MAINTENANCE_TIMEOUT_SECONDS = 5 * 60

_PLATFORM_EXCLUDES = ("node_modules/", "dist/", ".vite/")
_CHECKPOINT_KINDS = {PRE_TURN, POST_TURN, ADOPT}
_MANAGED_POINTER_PARENT = "show-git"
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAIN_ANCHOR_REF = "refs/avibe/checkpoint-main"
_TURN_STATE_KEY = "_avibe_show_git_checkpoint"
_CHECKPOINT_STATUS_VERSION = 1
_checkpoint_service_active: bool | None = None
_SCRUBBED_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG_PARAMETERS",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
    "EMAIL",
}

SHOW_GIT_AGENT_CONTRACT = (
    "History is saved automatically around each turn; do not manage versions yourself.",
    "Read freely: `git -C <workspace> status / log / diff / show`.",
    "Restore only via `git restore --source=<ref> -- <path>`; the turn-end checkpoint records it as a forward commit.",
    "Never move HEAD, switch branches, rewrite history, or run gc; if you do, the platform self-heals with the worktree as truth.",
    "Never add remotes, push, or publish the workspace anywhere unless the user explicitly asks.",
)
SHOW_GIT_SELF_MANAGED_AGENT_CONTRACT = (
    "Avibe's shadow history continues automatically in the background; you don't manage it.",
    "`git -C <workspace>` addresses the **user's repo**, not Avibe history: never commit, push, or publish on their behalf, and never use it for Avibe restore.",
    "Never locate or mutate Avibe's shadow gitdir on your own initiative. Only if the user explicitly asks to recover from Avibe history, use standard git with explicit `--git-dir` and `--work-tree` against the session's shadow gitdir for read or restore only; never commit to it.",
)
SHOW_GIT_UNAVAILABLE_AGENT_CONTRACT = (
    "Automatic Show Page history is unavailable because Git could not be resolved for this process. Continue editing normally, but do not use history or restore commands for this workspace.",
)


class ShowGitError(RuntimeError):
    """A platform Git operation failed."""


@dataclass(frozen=True)
class TurnCheckpointContext:
    message: str = ""
    run_id: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class _ActiveTurnCheckpoint:
    context: TurnCheckpointContext
    started_at: str


def _record_checkpoint_service_state(active: bool) -> None:
    global _checkpoint_service_active

    _checkpoint_service_active = active
    status_path = paths.get_show_git_runtime_status_path()
    temporary = status_path.with_name(f".{status_path.name}.{os.getpid()}.tmp")
    payload = {
        "version": _CHECKPOINT_STATUS_VERSION,
        "active": active,
        "service_pid": os.getpid(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, status_path)
    except OSError:
        logger.warning("failed to persist Show Page checkpoint service state", exc_info=True)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def show_git_checkpointing_active() -> bool:
    """Return the running service's startup-latched checkpoint capability."""

    if _checkpoint_service_active is not None:
        return _checkpoint_service_active
    try:
        payload = json.loads(paths.get_show_git_runtime_status_path().read_text(encoding="utf-8"))
        if payload.get("version") != _CHECKPOINT_STATUS_VERSION or payload.get("active") is not True:
            return False
        service_pid = int(payload.get("service_pid") or 0)
        from vibe.runtime import resolve_service_owner_pid

        return service_pid > 0 and resolve_service_owner_pid(include_starting=False) == service_pid
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _read_git_pointer(pointer: Path) -> Path | None:
    if pointer.is_symlink() or not pointer.is_file():
        return None
    try:
        lines = pointer.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) != 1 or not lines[0].startswith("gitdir: "):
        return None
    raw = lines[0].removeprefix("gitdir: ").strip()
    if not raw:
        return None
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = pointer.parent / target
    return target.resolve(strict=False)


def _workspace_ownership(workspace: Path, gitdir: Path) -> tuple[bool, bool]:
    pointer = workspace / ".git"
    if not pointer.exists() and not pointer.is_symlink():
        return True, False
    target = _read_git_pointer(pointer)
    if target is None:
        return False, False
    expected = gitdir.resolve(strict=False)
    if target == expected:
        return True, False
    if not target.exists() and target.name == gitdir.name and target.parent.name == _MANAGED_POINTER_PARENT:
        return True, True
    return False, False


def _workspace_is_self_managed(session_id: str) -> bool:
    if not _SESSION_ID_PATTERN.fullmatch(str(session_id or "")):
        return True
    managed, _rewrite_pointer = _workspace_ownership(
        paths.get_show_page_dir(session_id),
        paths.get_show_git_dir(session_id),
    )
    return not managed


def format_agent_contract(
    *,
    numbered: bool = False,
    checkpointing_available: bool | None = None,
    session_id: str | None = None,
) -> str:
    if checkpointing_available is None:
        checkpointing_available = show_git_checkpointing_active()
    if not checkpointing_available:
        lines = SHOW_GIT_UNAVAILABLE_AGENT_CONTRACT
    elif session_id is not None and _workspace_is_self_managed(session_id):
        lines = SHOW_GIT_SELF_MANAGED_AGENT_CONTRACT
    else:
        lines = SHOW_GIT_AGENT_CONTRACT
    if numbered:
        return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))
    return "\n".join(f"- {line}" for line in lines)


def _single_line(value: Any) -> str:
    without_controls = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in str(value or ""))
    return " ".join(without_controls.split())


def sanitize_checkpoint_subject(message: str | None) -> str:
    return _single_line(message)[:72].rstrip() or "checkpoint"


def _checkpoint_message(subject: str, *, session_id: str, run_id: str | None, checkpoint: str) -> str:
    safe_run_id = _single_line(run_id) or "-"
    return (
        f"{subject}\n\n"
        f"Avibe-Session: {session_id}\n"
        f"Avibe-Run: {safe_run_id}\n"
        f"Avibe-Checkpoint: {checkpoint}"
    )


def _git_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key in _SCRUBBED_GIT_ENV or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    env.pop("GIT_CONFIG_COUNT", None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return env


def _read_message_text(content_text: Any, content_json: Any) -> str:
    text = str(content_text or "").strip()
    if text:
        return text
    try:
        content = json.loads(content_json or "{}")
    except (TypeError, ValueError):
        return ""
    return str(content.get("text") or "").strip() if isinstance(content, dict) else ""


def _run_id_from_native_message_id(value: Any) -> str | None:
    native_id = str(value or "").strip()
    if not native_id.startswith("agent_run:"):
        return None
    run_id = native_id.removeprefix("agent_run:").strip()
    return run_id or None


def load_turn_checkpoint_context(session_id: str, *, after: str | None = None) -> TurnCheckpointContext:
    """Read the driving message/run without changing the turn event payload."""

    try:
        from sqlalchemy import select

        from storage.db import get_cached_sqlite_engine
        from storage.models import agent_runs, messages

        with get_cached_sqlite_engine().connect() as conn:
            active_run = conn.execute(
                select(agent_runs.c.id, agent_runs.c.message, agent_runs.c.prompt)
                .where(agent_runs.c.session_id == session_id)
                .where(agent_runs.c.status.in_(("running", "processing")))
                .where(agent_runs.c.run_type != "watch_runtime")
                .order_by(agent_runs.c.started_at.desc(), agent_runs.c.created_at.desc(), agent_runs.c.id.desc())
                .limit(1)
            ).first()
            if active_run is not None:
                text = str(active_run.message or active_run.prompt or "").strip()
                run_id = str(active_run.id)
                return TurnCheckpointContext(message=text, run_id=run_id, message_id=f"agent_run:{run_id}")

            message_query = select(
                messages.c.id,
                messages.c.type,
                messages.c.content_text,
                messages.c.content_json,
                messages.c.native_message_id,
            ).where(messages.c.session_id == session_id)
            if after is None:
                message_query = (
                    message_query.where(messages.c.type.not_in(("queued", "draft", "harness_dedupe")))
                    .order_by(messages.c.created_at.desc(), messages.c.id.desc())
                    .limit(1)
                )
            else:
                message_query = (
                    message_query.where(messages.c.type.in_(("user", "pending")))
                    .where(messages.c.created_at >= after)
                    .order_by(messages.c.created_at.asc(), messages.c.id.asc())
                    .limit(1)
                )
            message_row = conn.execute(message_query).first()
            if message_row is None or message_row.type not in {"user", "pending"}:
                return TurnCheckpointContext()
            return TurnCheckpointContext(
                message=_read_message_text(message_row.content_text, message_row.content_json),
                run_id=_run_id_from_native_message_id(message_row.native_message_id),
                message_id=str(message_row.id),
            )
    except Exception:
        logger.debug("show checkpoint metadata lookup failed for session=%s", session_id, exc_info=True)
        return TurnCheckpointContext()


class ShowGitRepository:
    def __init__(
        self,
        session_id: str,
        git: ResolvedGit,
        *,
        workspace: Path | None = None,
        gitdir: Path | None = None,
    ) -> None:
        if not _SESSION_ID_PATTERN.fullmatch(str(session_id or "")):
            raise ValueError("invalid Show Page session id")
        self.session_id = session_id
        self.git = git
        self.workspace = workspace or paths.get_show_page_dir(session_id)
        self.gitdir = gitdir or paths.get_show_git_dir(session_id)
        self.hooks_dir = self.gitdir / "avibe-empty-hooks"

    def checkpoint(self, checkpoint: str, *, message: str = "", run_id: str | None = None) -> bool:
        if checkpoint not in {PRE_TURN, POST_TURN}:
            raise ValueError(f"unsupported checkpoint kind: {checkpoint}")
        if not self.workspace.is_dir():
            return False

        managed, rewrite_pointer = self._workspace_ownership()
        initialized = self._ensure_repository()
        self._remove_stale_index_lock()
        self._ensure_platform_config()
        self._ensure_empty_hooks_dir()
        self._ensure_platform_excludes()
        if managed:
            self._ensure_pointer(rewrite=rewrite_pointer)
        self._reattach_main()
        self._restore_forward_main()

        if initialized or not self._has_commits():
            created = self._commit_all(
                "adopt existing workspace",
                run_id=run_id,
                checkpoint=ADOPT,
                allow_empty=True,
            )
        else:
            subject = "out-of-band changes" if checkpoint == PRE_TURN else sanitize_checkpoint_subject(message)
            created = self._commit_if_dirty(subject, run_id=run_id, checkpoint=checkpoint)

        self._prune_if_needed()
        self._record_main_anchor()
        return created

    def _workspace_ownership(self) -> tuple[bool, bool]:
        return _workspace_ownership(self.workspace, self.gitdir)

    def _ensure_repository(self) -> bool:
        valid = (self.gitdir / "HEAD").is_file() and (self.gitdir / "objects").is_dir()
        if valid:
            return False
        self.gitdir.parent.mkdir(parents=True, exist_ok=True)
        result = self._run_raw(
            ["init", "--bare", str(self.gitdir)],
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise ShowGitError(result.stderr.strip() or "failed to initialize Show Page checkpoint repository")
        self._run_checked(["symbolic-ref", "HEAD", "refs/heads/main"])
        return True

    def _ensure_platform_config(self) -> None:
        self._run_checked(["config", "core.bare", "false"])

    def _ensure_empty_hooks_dir(self) -> None:
        if self.hooks_dir.is_symlink() or (self.hooks_dir.exists() and not self.hooks_dir.is_dir()):
            self.hooks_dir.unlink()
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        for child in self.hooks_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _ensure_platform_excludes(self) -> None:
        exclude = self.gitdir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        except OSError as exc:
            raise ShowGitError(f"failed to read checkpoint excludes: {exc}") from exc
        existing_lines = set(existing.splitlines())
        missing = [pattern for pattern in _PLATFORM_EXCLUDES if pattern not in existing_lines]
        if not missing:
            return
        prefix = existing
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        exclude.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")

    def _ensure_pointer(self, *, rewrite: bool) -> None:
        pointer = self.workspace / ".git"
        if pointer.exists() or pointer.is_symlink():
            if not rewrite:
                return
            if pointer.is_symlink() or not pointer.is_file():
                return
        temporary = self.gitdir / f"avibe-git-pointer-{os.getpid()}.tmp"
        try:
            temporary.write_text(f"gitdir: {self.gitdir.resolve(strict=False)}\n", encoding="utf-8")
            os.replace(temporary, pointer)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_stale_index_lock(self) -> None:
        lock = self.gitdir / "index.lock"
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return
        if age <= STALE_INDEX_LOCK_SECONDS:
            return
        try:
            lock.unlink()
        except OSError:
            logger.warning("failed to remove stale Show Page checkpoint index lock: %s", lock, exc_info=True)

    def _reattach_main(self) -> None:
        current = self._run(["symbolic-ref", "-q", "HEAD"], check=False)
        if current.returncode == 0 and current.stdout.strip() == "refs/heads/main":
            return
        self._run_checked(["symbolic-ref", "HEAD", "refs/heads/main"])

    def _restore_forward_main(self) -> None:
        anchor = self._run(["rev-parse", "--verify", _MAIN_ANCHOR_REF], check=False)
        if anchor.returncode != 0:
            return
        anchor_oid = anchor.stdout.strip()
        current = self._run(["rev-parse", "--verify", "refs/heads/main"], check=False)
        if current.returncode != 0:
            self._run_checked(["update-ref", "refs/heads/main", anchor_oid])
            return
        current_oid = current.stdout.strip()
        ancestry = self._run(["merge-base", "--is-ancestor", anchor_oid, current_oid], check=False)
        if ancestry.returncode == 0:
            return
        if ancestry.returncode != 1:
            raise ShowGitError(ancestry.stderr.strip() or "failed to verify Show Page checkpoint ancestry")
        self._run_checked(["update-ref", "refs/heads/main", anchor_oid, current_oid])

    def _record_main_anchor(self) -> None:
        head = self._run_checked(["rev-parse", "refs/heads/main"]).stdout.strip()
        self._run_checked(["update-ref", _MAIN_ANCHOR_REF, head])

    def _has_commits(self) -> bool:
        return self._run(["rev-parse", "--verify", "HEAD"], check=False).returncode == 0

    def _commit_if_dirty(self, subject: str, *, run_id: str | None, checkpoint: str) -> bool:
        status = self._run_checked(["status", "--porcelain=v1", "--untracked-files=all"])
        if not status.stdout:
            return False
        return self._commit_all(subject, run_id=run_id, checkpoint=checkpoint, allow_empty=False)

    def _commit_all(self, subject: str, *, run_id: str | None, checkpoint: str, allow_empty: bool) -> bool:
        if checkpoint not in _CHECKPOINT_KINDS:
            raise ValueError(f"unsupported commit checkpoint kind: {checkpoint}")
        self._run_checked(["add", "-A"])
        command = ["commit"]
        if allow_empty:
            command.append("--allow-empty")
        command.extend(
            [
                "-m",
                _checkpoint_message(
                    subject,
                    session_id=self.session_id,
                    run_id=run_id,
                    checkpoint=checkpoint,
                ),
            ]
        )
        result = self._run(command, check=False)
        if result.returncode == 0:
            return True
        if not allow_empty and "nothing to commit" in result.stdout.lower():
            return False
        raise ShowGitError(result.stderr.strip() or result.stdout.strip() or "failed to commit Show Page checkpoint")

    def _prune_if_needed(self) -> None:
        if not self._has_commits():
            return
        count_result = self._run_checked(["rev-list", "--count", "refs/heads/main"])
        try:
            commit_count = int(count_result.stdout.strip())
        except ValueError:
            commit_count = 0
        if commit_count <= MAX_COMMITS and self._gitdir_size_at_most(MAX_GITDIR_BYTES):
            return

        remotes = self._run_checked(["remote"])
        if remotes.stdout.strip():
            self._run_checked(["gc"], timeout=GIT_MAINTENANCE_TIMEOUT_SECONDS)
            return

        old_head = self._run_checked(["rev-parse", "refs/heads/main"]).stdout.strip()
        tree = self._run_checked(["rev-parse", "refs/heads/main^{tree}"]).stdout.strip()
        message = self._run_checked(["log", "-1", "--format=%B", "refs/heads/main"]).stdout.rstrip()
        baseline = self._run_checked(["commit-tree", tree], input_text=message + "\n").stdout.strip()
        self._run_checked(["update-ref", "refs/heads/main", baseline, old_head])

        refs = self._run_checked(["for-each-ref", "--format=%(refname)"]).stdout.splitlines()
        for ref in refs:
            if ref and ref != "refs/heads/main":
                self._run_checked(["update-ref", "-d", ref])
        self._run_checked(["reflog", "expire", "--expire=now", "--all"])
        self._run_checked(["gc", "--prune=now"], timeout=GIT_MAINTENANCE_TIMEOUT_SECONDS)

    def _gitdir_size_at_most(self, limit: int) -> bool:
        total = 0
        try:
            for root, _dirs, files in os.walk(self.gitdir):
                for filename in files:
                    try:
                        total += (Path(root) / filename).stat().st_size
                    except OSError:
                        continue
                    if total > limit:
                        return False
        except OSError:
            return True
        return True

    def _base_command(self) -> list[str]:
        return [
            str(self.git.path),
            "-c",
            "user.name=avibe-checkpoint",
            "-c",
            "user.email=checkpoint@avibe.local",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={self.hooks_dir}",
            "-c",
            "gc.auto=0",
        ]

    def _run_raw(
        self,
        args: list[str],
        *,
        timeout: int,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [*self._base_command(), *args],
                check=False,
                capture_output=True,
                text=True,
                input=input_text,
                env=_git_environment(),
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ShowGitError(f"failed to execute Git checkpoint command: {exc}") from exc

    def _run(
        self,
        args: list[str],
        *,
        check: bool,
        timeout: int = GIT_COMMAND_TIMEOUT_SECONDS,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run_raw(
            [f"--git-dir={self.gitdir}", f"--work-tree={self.workspace}", *args],
            timeout=timeout,
            input_text=input_text,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
            raise ShowGitError(detail)
        return result

    def _run_checked(
        self,
        args: list[str],
        *,
        timeout: int = GIT_COMMAND_TIMEOUT_SECONDS,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(args, check=True, timeout=timeout, input_text=input_text)


_UNSET = object()


class ShowGitCheckpointService:
    """Synchronous turn-boundary subscriber owned by the Controller process."""

    def __init__(self, git: ResolvedGit | None | object = _UNSET) -> None:
        self.git = resolve_git() if git is _UNSET else git
        self._bus: Any = None
        self._subscription_id: int | None = None
        self._active_turns: dict[str, _ActiveTurnCheckpoint] = {}
        self._open_bus_turns: set[str] = set()

    @property
    def enabled(self) -> bool:
        return isinstance(self.git, ResolvedGit)

    def start(self, event_bus: Any = None) -> None:
        if self._subscription_id is not None:
            return
        _record_checkpoint_service_state(False)
        if not self.enabled:
            return
        if event_bus is None:
            from core.inbox_events import bus as event_bus

        self._bus = event_bus
        self._subscription_id = event_bus.subscribe_callback(self._handle_event)
        _record_checkpoint_service_state(True)

    def stop(self) -> None:
        global _checkpoint_service_active

        if self._bus is not None and self._subscription_id is not None:
            self._bus.unsubscribe(self._subscription_id)
        self._bus = None
        self._subscription_id = None
        self._active_turns.clear()
        self._open_bus_turns.clear()
        _record_checkpoint_service_state(False)
        _checkpoint_service_active = None

    @staticmethod
    def _turn_state(context: Any) -> dict[str, Any] | None:
        state = (getattr(context, "platform_specific", None) or {}).get(_TURN_STATE_KEY)
        return state if isinstance(state, dict) else None

    @staticmethod
    def _existing_session_id(controller: Any, context: Any) -> str | None:
        session_id = controller._session_id_from_context(context)
        if session_id:
            return session_id
        payload = getattr(context, "platform_specific", None) or {}
        base_session_id = str(payload.get("turn_base_session_id") or "").strip()
        finder = getattr(getattr(controller, "sessions", None), "find_session_for_anchor", None)
        if not base_session_id or not callable(finder):
            return None
        try:
            row = finder(controller._get_session_key(context), base_session_id)
        except Exception:
            logger.debug("Show checkpoint session lookup failed", exc_info=True)
            return None
        session_id = str(row.get("id") or "").strip() if row else ""
        if not session_id:
            return None
        updated = dict(payload)
        updated["agent_session_id"] = session_id
        context.platform_specific = updated
        return session_id

    @staticmethod
    def _link_message(context: Any, session_id: str) -> bool:
        platform = str(getattr(context, "platform", None) or "").strip()
        message_id = str(getattr(context, "message_id", None) or "").strip()
        if not platform or platform == "avibe" or not message_id:
            return False
        try:
            from core.message_mirror import link_inbound_message_session

            link_inbound_message_session(
                platform=platform,
                native_message_id=message_id,
                session_id=session_id,
            )
            return True
        except Exception:
            logger.debug("Show checkpoint message link failed", exc_info=True)
            return False

    def begin_turn(self, controller: Any, context: Any) -> None:
        """Publish checkpoint start from the shared backend execution boundary."""

        if self._bus is None:
            return
        state = self._turn_state(context)
        if state is None or state.get("ended"):
            state = {"start_observed": False, "ended": False}
        if state.get("start_observed"):
            return
        payload = dict(getattr(context, "platform_specific", None) or {})
        payload[_TURN_STATE_KEY] = state
        context.platform_specific = payload
        session_id = self._existing_session_id(controller, context)
        if not session_id:
            # A first-ever turn can bind its session only after backend dispatch.
            # Keep the state on the context so terminal delivery can lazily adopt
            # a workspace created during that turn.
            return
        owns_bus_lifecycle = session_id not in self._open_bus_turns
        state = {
            **state,
            "start_observed": True,
            "start_session_id": session_id,
            "owns_bus_lifecycle": owns_bus_lifecycle,
            "message_linked": self._link_message(context, session_id),
        }
        payload = dict(context.platform_specific or {})
        payload[_TURN_STATE_KEY] = state
        context.platform_specific = payload
        # Workbench and /internal/dispatch may already have published their UI
        # lifecycle before backend execution reached this shared boundary.
        if owns_bus_lifecycle:
            self._bus.publish("turn.start", {"session_id": session_id})

    def end_turn(self, context: Any) -> None:
        """Publish checkpoint end from the shared terminal-result boundary."""

        state = self._turn_state(context)
        if self._bus is None or state is None or state.get("ended"):
            return
        payload = dict(getattr(context, "platform_specific", None) or {})
        session_id = str(payload.get("agent_session_id") or state.get("start_session_id") or "").strip()
        message_linked = bool(state.get("message_linked"))
        if session_id and not message_linked:
            message_linked = self._link_message(context, session_id)
        payload[_TURN_STATE_KEY] = {**state, "ended": True, "message_linked": message_linked}
        context.platform_specific = payload
        if session_id and (not state.get("start_observed") or state.get("owns_bus_lifecycle")):
            self._bus.publish("turn.end", {"session_id": session_id})

    def _handle_event(self, event_type: str, data: Any) -> None:
        if event_type not in {"turn.start", "turn.end"} or not isinstance(data, dict):
            return
        session_id = str(data.get("session_id") or "").strip()
        if not _SESSION_ID_PATTERN.fullmatch(session_id):
            return
        if event_type == "turn.start":
            self._open_bus_turns.add(session_id)
        else:
            self._open_bus_turns.discard(session_id)
        if not paths.get_show_page_dir(session_id).is_dir():
            if event_type == "turn.end":
                self._active_turns.pop(session_id, None)
            return
        try:
            if event_type == "turn.start":
                started_at = datetime.now(timezone.utc).isoformat()
                context = load_turn_checkpoint_context(session_id)
                self._active_turns[session_id] = _ActiveTurnCheckpoint(context=context, started_at=started_at)
                self._repository(session_id).checkpoint(PRE_TURN, run_id=context.run_id)
                return

            active = self._active_turns.pop(session_id, None)
            if active is None:
                context = load_turn_checkpoint_context(session_id)
                start_context = TurnCheckpointContext()
            else:
                start_context = active.context
                context = (
                    TurnCheckpointContext()
                    if start_context.message_id is not None
                    else load_turn_checkpoint_context(session_id, after=active.started_at)
                )
            run_id = start_context.run_id or context.run_id
            self._repository(session_id).checkpoint(
                POST_TURN,
                message=start_context.message if start_context.message_id is not None else context.message,
                run_id=run_id,
            )
        except Exception:
            logger.exception("Show Page checkpoint failed at %s for session=%s", event_type, session_id)

    def _repository(self, session_id: str) -> ShowGitRepository:
        if not isinstance(self.git, ResolvedGit):
            raise ShowGitError("Git is unavailable")
        return ShowGitRepository(session_id, self.git)
