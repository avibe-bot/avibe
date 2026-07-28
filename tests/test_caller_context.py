from __future__ import annotations

import pytest

from core import caller_context as caller_context_module
from core.caller_context import (
    AVIBE_AUTHORIZATION_CAPABILITY_ENV,
    AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
    AVIBE_CALLER_BACKEND_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_HARNESS_AUTHORIZATION_ENV,
    AVIBE_NATIVE_SESSION_ID_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
    caller_context_from_env,
    caller_context_from_platform_payload,
    caller_env_for_platform_payload,
    issue_authorization_capability,
    retire_authorization_capabilities,
    resolve_authorization_capability,
)


def test_caller_context_from_env_requires_session_id() -> None:
    assert caller_context_from_env({}) is None


def test_caller_context_from_env_round_trips_metadata_and_env() -> None:
    context = caller_context_from_env(
        {
            AVIBE_SESSION_ID_ENV: "ses123",
            AVIBE_RUN_ID_ENV: "run456",
            AVIBE_CALLER_SOURCE_ENV: "agent_run",
            AVIBE_CALLER_BACKEND_ENV: "codex",
            AVIBE_NATIVE_SESSION_ID_ENV: "thread789",
        }
    )

    assert context is not None
    assert context.to_metadata() == {
        "session_id": "ses123",
        "run_id": "run456",
        "source": "agent_run",
        "backend": "codex",
        "native_session_id": "thread789",
    }
    caller_env = context.to_env()
    assert caller_env[AVIBE_SESSION_ID_ENV] == "ses123"
    assert "PATH" not in caller_env


def test_caller_context_from_platform_payload_prefers_agent_session_target() -> None:
    context = caller_context_from_platform_payload(
        {
            "agent_session_id": "legacy",
            "task_execution_id": "run123",
            "task_trigger_kind": "agent_run",
            "agent_session_target": {
                "id": "ses-target",
                "agent_backend": "opencode",
                "native_session_id": "oc-session",
            },
        }
    )

    assert context is not None
    assert context.to_metadata() == {
        "session_id": "ses-target",
        "run_id": "run123",
        "source": "agent_run",
        "backend": "opencode",
        "native_session_id": "oc-session",
    }
    assert context.to_env()[AVIBE_NATIVE_SESSION_ID_ENV] == "oc-session"


def test_caller_context_from_platform_payload_preserves_callback_source() -> None:
    context = caller_context_from_platform_payload(
        {
            "agent_session_id": "ses-callback",
            "task_execution_id": "run-callback",
            "task_trigger_kind": "agent_run",
            "source_kind": "callback",
        }
    )

    assert context is not None
    assert context.source == "callback"


def test_caller_context_transports_normalized_remote_principal(monkeypatch) -> None:
    from core import caller_context as caller_context_module
    from vibe import internal_client

    context = caller_context_from_platform_payload(
        {
            "agent_session_id": "ses-remote",
            "harness_execution_principal": {
                "principal_type": "remote",
                "instance_id": "instance-remote",
                "subject": "member-remote",
                "organization_member_id": "organization-member-remote",
                "membership_version": "membership-v2",
                "ignored_role": "owner",
            },
        }
    )

    assert context is not None
    principal = {
        "principal_type": "remote",
        "instance_id": "instance-remote",
        "subject": "member-remote",
        "organization_member_id": "organization-member-remote",
        "membership_version": "membership-v2",
    }
    assert context.authorization_principal == principal
    monkeypatch.setattr(
        internal_client,
        "resolve_authorization_principal_capability",
        caller_context_module.resolve_authorization_capability,
    )
    caller_env = caller_env_for_platform_payload(
        {
            "agent_session_id": "ses-remote",
            "harness_execution_principal": principal,
        }
    )
    assert caller_env[AVIBE_AUTHORIZATION_CAPABILITY_ENV]
    assert AVIBE_AUTHORIZATION_PRINCIPAL_ENV not in caller_env
    restored = caller_context_from_env(caller_env)
    assert restored is not None
    assert restored.authorization_principal == principal


def test_authorization_capability_expires_closed() -> None:
    token = issue_authorization_capability(
        {
            "principal_type": "remote",
            "instance_id": "instance-expiring",
            "subject": "member-expiring",
        },
        session_id="session-expiring",
        now=100.0,
    )

    assert resolve_authorization_capability(
        token,
        session_id="session-expiring",
        now=399.0,
    )["subject"] == "member-expiring"
    with pytest.raises(ValueError, match="invalid authorization principal capability"):
        resolve_authorization_capability(
            token,
            session_id="session-expiring",
            now=400.0,
        )


def test_authorization_capability_lives_until_bound_turn_retires() -> None:
    token = issue_authorization_capability(
        {
            "principal_type": "remote",
            "instance_id": "instance-active-turn",
            "subject": "member-active-turn",
        },
        session_id="session-active-turn",
        runtime_turn_token="runtime-active-turn",
        now=100.0,
    )

    assert resolve_authorization_capability(
        token,
        session_id="session-active-turn",
        now=10_000.0,
    )["subject"] == "member-active-turn"

    retire_authorization_capabilities("runtime-active-turn")

    with pytest.raises(ValueError, match="invalid authorization principal capability"):
        resolve_authorization_capability(
            token,
            session_id="session-active-turn",
            now=10_000.0,
        )


def test_caller_context_recovers_capability_when_agent_strips_child_env(
    monkeypatch,
) -> None:
    from vibe import internal_client

    principal = {
        "principal_type": "remote",
        "instance_id": "instance-ancestor",
        "subject": "member-ancestor",
    }
    capability = issue_authorization_capability(
        principal,
        session_id="session-ancestor",
    )
    for key in (
        AVIBE_SESSION_ID_ENV,
        AVIBE_RUN_ID_ENV,
        AVIBE_HARNESS_AUTHORIZATION_ENV,
        AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
        AVIBE_CALLER_SOURCE_ENV,
        AVIBE_CALLER_BACKEND_ENV,
        AVIBE_NATIVE_SESSION_ID_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        caller_context_module,
        "_ancestor_caller_environments",
        lambda: [
            {
                AVIBE_SESSION_ID_ENV: "session-ancestor",
                AVIBE_AUTHORIZATION_CAPABILITY_ENV: capability,
                AVIBE_CALLER_SOURCE_ENV: "agent_turn",
                AVIBE_CALLER_BACKEND_ENV: "codex",
            }
        ],
    )
    monkeypatch.setattr(
        internal_client,
        "resolve_authorization_principal_capability",
        resolve_authorization_capability,
    )

    context = caller_context_from_env()

    assert context is not None
    assert context.session_id == "session-ancestor"
    assert context.backend == "codex"
    assert context.authorization_principal == principal
