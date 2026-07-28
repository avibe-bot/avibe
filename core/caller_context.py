"""Avibe caller-context contract for Agent-initiated Harness calls."""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping, Optional

AVIBE_SESSION_ID_ENV = "AVIBE_SESSION_ID"
AVIBE_RUN_ID_ENV = "AVIBE_RUN_ID"
AVIBE_HARNESS_AUTHORIZATION_ENV = "AVIBE_HARNESS_AUTHORIZATION"
# Kept so older subprocess environments are recognized and rejected closed.
AVIBE_AUTHORIZATION_PRINCIPAL_ENV = "AVIBE_AUTHORIZATION_PRINCIPAL"
AVIBE_AUTHORIZATION_CAPABILITY_ENV = "AVIBE_AUTHORIZATION_CAPABILITY"
AVIBE_CALLER_SOURCE_ENV = "AVIBE_CALLER_SOURCE"
AVIBE_CALLER_BACKEND_ENV = "AVIBE_CALLER_BACKEND"
AVIBE_NATIVE_SESSION_ID_ENV = "AVIBE_NATIVE_SESSION_ID"

_AUTHORIZATION_CAPABILITY_TTL_SECONDS = 5 * 60
_AUTHORIZATION_CAPABILITY_LIMIT = 4096
_authorization_capabilities: dict[
    str,
    tuple[float | None, str, Optional[str], dict[str, str], str | None],
] = {}
_authorization_capabilities_lock = RLock()
_CALLER_ENV_KEYS = (
    AVIBE_SESSION_ID_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_HARNESS_AUTHORIZATION_ENV,
    AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
    AVIBE_AUTHORIZATION_CAPABILITY_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_CALLER_BACKEND_ENV,
    AVIBE_NATIVE_SESSION_ID_ENV,
)


@dataclass(frozen=True)
class CallerContext:
    """Caller identity resolved from Avibe-owned execution context."""

    session_id: str
    run_id: Optional[str] = None
    source: Optional[str] = None
    backend: Optional[str] = None
    native_session_id: Optional[str] = None
    authorization_principal: Optional[dict[str, str]] = None

    def to_env(self) -> dict[str, str]:
        env = {AVIBE_SESSION_ID_ENV: self.session_id}
        if self.run_id:
            env[AVIBE_RUN_ID_ENV] = self.run_id
            env[AVIBE_HARNESS_AUTHORIZATION_ENV] = "1"
        if self.source:
            env[AVIBE_CALLER_SOURCE_ENV] = self.source
        if self.backend:
            env[AVIBE_CALLER_BACKEND_ENV] = self.backend
        if self.native_session_id:
            env[AVIBE_NATIVE_SESSION_ID_ENV] = self.native_session_id
        return env

    def to_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {"session_id": self.session_id}
        if self.run_id:
            metadata["run_id"] = self.run_id
        if self.source:
            metadata["source"] = self.source
        if self.backend:
            metadata["backend"] = self.backend
        if self.native_session_id:
            metadata["native_session_id"] = self.native_session_id
        if self.authorization_principal:
            metadata["authorization_principal"] = dict(self.authorization_principal)
        return metadata


def _clean(value: object) -> str:
    return str(value or "").strip()


def _authorization_principal(value: object) -> Optional[dict[str, str]]:
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("principal_type") != "remote":
        raise ValueError("invalid authorization principal")
    instance_id = _clean(value.get("instance_id"))
    subject = _clean(value.get("subject"))
    if not instance_id or not subject:
        raise ValueError("invalid authorization principal")
    principal = {
        "principal_type": "remote",
        "instance_id": instance_id,
        "subject": subject,
    }
    for key in ("organization_member_id", "membership_version"):
        cleaned = _clean(value.get(key))
        if cleaned:
            principal[key] = cleaned
    return principal


def issue_authorization_capability(
    principal: Mapping[str, object],
    *,
    session_id: str,
    run_id: str | None = None,
    runtime_turn_token: str | None = None,
    now: float | None = None,
) -> str:
    """Register a controller-memory capability for one Agent turn."""

    normalized_principal = _authorization_principal(principal)
    normalized_session_id = _clean(session_id)
    normalized_run_id = _clean(run_id) or None
    normalized_runtime_turn_token = _clean(runtime_turn_token) or None
    if normalized_principal is None or not normalized_session_id:
        raise ValueError("invalid authorization principal capability")
    current = time.monotonic() if now is None else float(now)
    token = secrets.token_urlsafe(32)
    with _authorization_capabilities_lock:
        expired = [
            candidate
            for candidate, (expires_at, *_rest) in _authorization_capabilities.items()
            if expires_at is not None and expires_at <= current
        ]
        for candidate in expired:
            _authorization_capabilities.pop(candidate, None)
        while len(_authorization_capabilities) >= _AUTHORIZATION_CAPABILITY_LIMIT:
            oldest = min(
                _authorization_capabilities,
                key=lambda candidate: (
                    _authorization_capabilities[candidate][0]
                    if _authorization_capabilities[candidate][0] is not None
                    else float("inf")
                ),
            )
            _authorization_capabilities.pop(oldest, None)
        _authorization_capabilities[token] = (
            (
                None
                if normalized_runtime_turn_token
                else current + _AUTHORIZATION_CAPABILITY_TTL_SECONDS
            ),
            normalized_session_id,
            normalized_run_id,
            normalized_principal,
            normalized_runtime_turn_token,
        )
    return token


def resolve_authorization_capability(
    token: str,
    *,
    session_id: str,
    run_id: str | None = None,
    now: float | None = None,
) -> dict[str, str]:
    """Resolve a capability inside the controller process only."""

    normalized_token = _clean(token)
    normalized_session_id = _clean(session_id)
    normalized_run_id = _clean(run_id) or None
    if not normalized_token or not normalized_session_id:
        raise ValueError("invalid authorization principal capability")
    current = time.monotonic() if now is None else float(now)
    with _authorization_capabilities_lock:
        record = _authorization_capabilities.get(normalized_token)
        if record is None:
            raise ValueError("invalid authorization principal capability")
        expires_at, bound_session_id, bound_run_id, principal, _turn_token = record
        if expires_at is not None and expires_at <= current:
            _authorization_capabilities.pop(normalized_token, None)
            raise ValueError("invalid authorization principal capability")
        if bound_session_id != normalized_session_id or bound_run_id != normalized_run_id:
            raise ValueError("invalid authorization principal capability")
        return dict(principal)


def retire_authorization_capabilities(runtime_turn_token: str) -> None:
    """Revoke every capability issued for one completed Agent turn."""

    normalized_turn_token = _clean(runtime_turn_token)
    if not normalized_turn_token:
        return
    with _authorization_capabilities_lock:
        retired = [
            token
            for token, (*_rest, bound_turn_token) in _authorization_capabilities.items()
            if bound_turn_token == normalized_turn_token
        ]
        for token in retired:
            _authorization_capabilities.pop(token, None)


def _ancestor_caller_environments() -> list[Mapping[str, str]]:
    """Read immutable ancestor environments for stripped Agent markers."""

    try:
        import psutil

        process = psutil.Process().parent()
        environments: list[Mapping[str, str]] = []
        for _ in range(16):
            if process is None:
                break
            environments.append(process.environ())
            process = process.parent()
        return environments
    except Exception:
        return []


def caller_context_environment(
    env: Mapping[str, str] | None = None,
) -> Mapping[str, str]:
    """Recover Avibe markers from an Agent's immutable process ancestry."""

    if env is not None:
        return env
    source = dict(os.environ)
    if _clean(source.get(AVIBE_AUTHORIZATION_CAPABILITY_ENV)):
        return source
    for ancestor in _ancestor_caller_environments():
        session_id = _clean(ancestor.get(AVIBE_SESSION_ID_ENV))
        carries_authorization = bool(
            _clean(ancestor.get(AVIBE_AUTHORIZATION_CAPABILITY_ENV))
            or _clean(ancestor.get(AVIBE_AUTHORIZATION_PRINCIPAL_ENV))
            or (
                _clean(ancestor.get(AVIBE_RUN_ID_ENV))
                and ancestor.get(AVIBE_HARNESS_AUTHORIZATION_ENV) == "1"
            )
        )
        if not session_id or not carries_authorization:
            continue
        for key in _CALLER_ENV_KEYS:
            value = ancestor.get(key)
            if value:
                source[key] = value
            else:
                source.pop(key, None)
        return source
    return source


def authorization_principal_from_env(
    env: Mapping[str, str] | None = None,
) -> Optional[dict[str, str]]:
    source = caller_context_environment(env)
    token = _clean(source.get(AVIBE_AUTHORIZATION_CAPABILITY_ENV))
    legacy_principal = _clean(source.get(AVIBE_AUTHORIZATION_PRINCIPAL_ENV))
    if not token and not legacy_principal:
        return None
    session_id = _clean(source.get(AVIBE_SESSION_ID_ENV))
    if not token or not session_id:
        raise ValueError("invalid authorization principal capability")
    from vibe.internal_client import resolve_authorization_principal_capability

    principal = resolve_authorization_principal_capability(
        token,
        session_id=session_id,
        run_id=_clean(source.get(AVIBE_RUN_ID_ENV)) or None,
    )
    return _authorization_principal(principal)


def caller_context_from_env(env: Mapping[str, str] | None = None) -> Optional[CallerContext]:
    """Resolve caller context from process env.

    The raw session id is authoritative only when Avibe injected it into an
    Agent subprocess. If it is absent, callers should fail or require explicit
    flags instead of guessing from native backend ids.
    """

    source = caller_context_environment(env)
    session_id = _clean(source.get(AVIBE_SESSION_ID_ENV))
    if not session_id:
        return None
    return CallerContext(
        session_id=session_id,
        run_id=_clean(source.get(AVIBE_RUN_ID_ENV)) or None,
        source=_clean(source.get(AVIBE_CALLER_SOURCE_ENV)) or None,
        backend=_clean(source.get(AVIBE_CALLER_BACKEND_ENV)) or None,
        native_session_id=_clean(source.get(AVIBE_NATIVE_SESSION_ID_ENV)) or None,
        authorization_principal=authorization_principal_from_env(source),
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
    authorization_principal = _authorization_principal(
        payload.get("harness_execution_principal")
    )
    return CallerContext(
        session_id=session_id,
        run_id=run_id or None,
        source=source or None,
        backend=backend or None,
        native_session_id=native_session_id or None,
        authorization_principal=authorization_principal,
    )


def caller_env_for_platform_payload(payload: Mapping[str, object] | None) -> dict[str, str]:
    context = caller_context_from_platform_payload(payload)
    if context is None:
        return {}
    env = context.to_env()
    if context.authorization_principal:
        env[AVIBE_AUTHORIZATION_CAPABILITY_ENV] = issue_authorization_capability(
            context.authorization_principal,
            session_id=context.session_id,
            run_id=context.run_id,
            runtime_turn_token=_clean(payload.get("agent_runtime_turn_token")) or None,
        )
    return env
