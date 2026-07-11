from __future__ import annotations

from core.caller_context import (
    AVIBE_CALLER_BACKEND_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_NATIVE_SESSION_ID_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
    CallerContext,
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
    assert context.to_env()[AVIBE_SESSION_ID_ENV] == "ses123"


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


def test_caller_context_env_uses_shared_git_runtime_path_seam(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    def fake_prepend(env) -> bool:
        calls.append(env)
        env["PATH"] = "/managed/git/bin:/usr/bin"
        return True

    monkeypatch.setattr("core.git_runtime.prepend_vendored_git_to_path", fake_prepend)

    env = CallerContext(session_id="ses123", backend="codex").to_env()

    assert calls == [env]
    assert env["PATH"].startswith("/managed/git/bin")
