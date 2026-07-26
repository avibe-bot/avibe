"""Avibe caller-context contract for Agent-initiated Harness calls."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping, Optional

AVIBE_SESSION_ID_ENV = "AVIBE_SESSION_ID"
AVIBE_RUN_ID_ENV = "AVIBE_RUN_ID"
AVIBE_CALLER_SOURCE_ENV = "AVIBE_CALLER_SOURCE"
AVIBE_CALLER_BACKEND_ENV = "AVIBE_CALLER_BACKEND"
AVIBE_NATIVE_SESSION_ID_ENV = "AVIBE_NATIVE_SESSION_ID"
AVIBE_MEMORY_CLI_CAPABILITY_ENV = "AVIBE_MEMORY_CLI_CAPABILITY"

# Caller context is not inert identity: ``to_env`` carries the Memory CLI
# capability, which ``core/memory/cli_access.py`` accepts as proof that a caller
# speaks for one principal. Every bridge that persists this env so a long-lived
# agent process can re-read it therefore writes a credential to disk, and the
# default umask would leave those files 0644 inside 0755 directories — readable
# by every other local user on the machine. Route such writes through
# ``write_private_caller_context_file`` so the bytes never exist world-readable.
CALLER_CONTEXT_DIR_MODE = 0o700
CALLER_CONTEXT_FILE_MODE = 0o600


@dataclass(frozen=True)
class CallerContext:
    """Caller identity resolved from Avibe-owned execution context."""

    session_id: str
    run_id: Optional[str] = None
    source: Optional[str] = None
    backend: Optional[str] = None
    native_session_id: Optional[str] = None
    memory_cli_capability: Optional[str] = None

    def to_env(self) -> dict[str, str]:
        env = {AVIBE_SESSION_ID_ENV: self.session_id}
        if self.run_id:
            env[AVIBE_RUN_ID_ENV] = self.run_id
        if self.source:
            env[AVIBE_CALLER_SOURCE_ENV] = self.source
        if self.backend:
            env[AVIBE_CALLER_BACKEND_ENV] = self.backend
        if self.native_session_id:
            env[AVIBE_NATIVE_SESSION_ID_ENV] = self.native_session_id
        if self.memory_cli_capability:
            env[AVIBE_MEMORY_CLI_CAPABILITY_ENV] = self.memory_cli_capability
        return env

    def to_metadata(self) -> dict[str, str]:
        metadata = {"session_id": self.session_id}
        if self.run_id:
            metadata["run_id"] = self.run_id
        if self.source:
            metadata["source"] = self.source
        if self.backend:
            metadata["backend"] = self.backend
        if self.native_session_id:
            metadata["native_session_id"] = self.native_session_id
        return metadata


def _clean(value: object) -> str:
    return str(value or "").strip()


def caller_context_from_env(env: Mapping[str, str] | None = None) -> Optional[CallerContext]:
    """Resolve caller context from process env.

    The raw session id is authoritative only when Avibe injected it into an
    Agent subprocess. If it is absent, callers should fail or require explicit
    flags instead of guessing from native backend ids.
    """

    source = env if env is not None else os.environ
    session_id = _clean(source.get(AVIBE_SESSION_ID_ENV))
    if not session_id:
        return None
    return CallerContext(
        session_id=session_id,
        run_id=_clean(source.get(AVIBE_RUN_ID_ENV)) or None,
        source=_clean(source.get(AVIBE_CALLER_SOURCE_ENV)) or None,
        backend=_clean(source.get(AVIBE_CALLER_BACKEND_ENV)) or None,
        native_session_id=_clean(source.get(AVIBE_NATIVE_SESSION_ID_ENV)) or None,
        memory_cli_capability=_clean(source.get(AVIBE_MEMORY_CLI_CAPABILITY_ENV)) or None,
    )


def caller_context_from_platform_payload(payload: Mapping[str, object] | None) -> Optional[CallerContext]:
    """Resolve caller context from an Avibe message/turn payload."""

    if not payload:
        return None
    target = payload.get("agent_session_target")
    session_id = ""
    backend = ""
    native_session_id = ""
    if isinstance(target, Mapping):
        session_id = _clean(target.get("id"))
        backend = _clean(target.get("agent_backend") or target.get("backend"))
        native_session_id = _clean(target.get("native_session_id"))
    session_id = session_id or _clean(payload.get("agent_session_id"))
    if not session_id:
        return None
    run_id = _clean(payload.get("task_execution_id"))
    source_kind = _clean(payload.get("source_kind"))
    trigger_kind = _clean(payload.get("task_trigger_kind"))
    source = source_kind if source_kind == "callback" else trigger_kind or source_kind or "agent_turn"
    backend = backend or _clean(payload.get("vibe_agent_backend"))
    return CallerContext(
        session_id=session_id,
        run_id=run_id or None,
        source=source or None,
        backend=backend or None,
        native_session_id=native_session_id or None,
        memory_cli_capability=_clean(payload.get("memory_cli_capability")) or None,
    )


def caller_env_for_platform_payload(payload: Mapping[str, object] | None) -> dict[str, str]:
    context = caller_context_from_platform_payload(payload)
    return context.to_env() if context else {}


def ensure_private_caller_context_dir(directory: Path) -> Path:
    """Narrow a directory Avibe owns exclusively for caller-context files.

    Only for directories whose *names* are themselves sensitive — the Codex
    bridge names each file after an Agent session id, so 0600 files inside a
    0755 directory would still enumerate live sessions. Never call this on a
    shared root such as ``runtime/``, where other components expect the
    directory mode they created.
    """

    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, CALLER_CONTEXT_DIR_MODE)
    except OSError:
        # Filesystems without POSIX modes keep whatever they support.
        pass
    return directory


def write_private_caller_context_file(path: Path, text: str) -> None:
    """Persist caller-context bytes so only the owning user can read them.

    The mode is applied to the temporary file before the rename, so the
    credential is never observable at 0644 even for the width of one write.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CALLER_CONTEXT_FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        os.chmod(tmp_path, CALLER_CONTEXT_FILE_MODE)
    except OSError:
        pass
    tmp_path.replace(path)
