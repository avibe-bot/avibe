"""Avibe caller-context contract for Agent-initiated Harness calls."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping, Optional

AVIBE_SESSION_ID_ENV = "AVIBE_SESSION_ID"
AVIBE_RUN_ID_ENV = "AVIBE_RUN_ID"
AVIBE_CALLER_SOURCE_ENV = "AVIBE_CALLER_SOURCE"
AVIBE_CALLER_BACKEND_ENV = "AVIBE_CALLER_BACKEND"
AVIBE_NATIVE_SESSION_ID_ENV = "AVIBE_NATIVE_SESSION_ID"


@dataclass(frozen=True)
class CallerContext:
    """Caller identity resolved from Avibe-owned execution context."""

    session_id: str
    run_id: Optional[str] = None
    source: Optional[str] = None
    backend: Optional[str] = None
    native_session_id: Optional[str] = None

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
    )


def caller_env_for_platform_payload(payload: Mapping[str, object] | None) -> dict[str, str]:
    context = caller_context_from_platform_payload(payload)
    return context.to_env() if context else {}
