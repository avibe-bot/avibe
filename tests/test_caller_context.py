from __future__ import annotations

import json

from core.caller_context import (
    AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
    AVIBE_CALLER_BACKEND_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_NATIVE_SESSION_ID_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
    caller_context_from_env,
    caller_context_from_platform_payload,
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


def test_caller_context_transports_normalized_remote_principal() -> None:
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
    assert json.loads(context.to_env()[AVIBE_AUTHORIZATION_PRINCIPAL_ENV]) == principal
    restored = caller_context_from_env(context.to_env())
    assert restored is not None
    assert restored.authorization_principal == principal
