"""Regression tests for the Settings → Backends web OAuth flow.

Pins the public surface added in PR #282 R5 so the Claude/Codex Settings
page can drive OAuth from the browser instead of asking users to copy a
``claude login`` command into a terminal. The state machine itself
(``WebAuthFlow``) and the four web methods on ``AgentAuthService`` are
exercised without spawning real subprocesses by injecting stub flows
into ``_web_flows`` and mocking ``_send_claude_callback``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent_auth_service import (
    AgentAuthService,
    ClaudeOAuthAttempt,
    ClaudeOAuthBatch,
    WebAuthFlow,
)
from modules.agents.opencode.message_processor import (
    extract_opencode_response_text,
    is_empty_terminal_opencode_message,
)
from vibe.claude_config import (
    MANAGED_ENV_VALUES,
    clear_claude_oauth_credentials_files,
    get_claude_credentials_path,
    read_claude_oauth_settings_backup,
    read_claude_settings_env,
    restore_claude_settings_env,
)


class _Backend:
    cli_path = "/usr/bin/echo"  # any binary that exists is fine


class _Agents:
    claude = _Backend
    codex = _Backend
    opencode = _Backend


class _Config:
    agents = _Agents()
    language = "en"
    runtime = None


class _StubController:
    """Minimal controller stand-in (see ``vibe/api.py::_WebControllerStub``)."""

    agent_service = None
    session_handler = None
    im_client = None
    config = _Config()


@pytest.fixture(autouse=True)
def isolated_claude_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude_home = tmp_path / "default-claude"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))


@pytest.fixture
def service() -> AgentAuthService:
    return AgentAuthService(_StubController())


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


async def _start_opencode_flow_without_waiter(
    service: AgentAuthService, provider_id: str
) -> WebAuthFlow:
    flow = await service.start_web_setup("opencode", provider_id=provider_id)
    if flow.waiter_task is not None:
        flow.waiter_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await flow.waiter_task
    return flow


def test_opencode_message_text_extractor_ignores_non_text_by_default() -> None:
    message = {
        "parts": [
            {
                "type": "reasoning",
                "text": "internal chain-of-thought-ish content",
            }
        ]
    }

    assert extract_opencode_response_text(message) == ""
    assert (
        extract_opencode_response_text(message, allow_non_text_fallback=True)
        == "internal chain-of-thought-ish content"
    )


def test_empty_terminal_opencode_message_treats_blank_text_as_empty() -> None:
    message = {
        "info": {
            "id": "msg_blank",
            "role": "assistant",
            "time": {"completed": 123},
            "finish": "unknown",
            "tokens": {
                "input": 8,
                "output": 4,
                "reasoning": 2,
                "cache": {"read": 1, "write": 0},
            },
        },
        "parts": [{"type": "text", "text": " \n\t "}],
    }

    assert is_empty_terminal_opencode_message(message) is True


def test_unsupported_backend_raises(service: AgentAuthService) -> None:
    with pytest.raises(ValueError, match="unsupported_backend"):
        _run(service.start_web_setup("gemini"))


def test_opencode_requires_provider_id(service: AgentAuthService) -> None:
    """OpenCode auth is per-provider; ``start_web_setup`` must reject a
    bare backend name with no ``provider_id`` rather than crash deep in
    the OAuth bootstrap. (The error is surfaced as a failed flow record
    so the UI can render a clear sentence.)"""
    flow = _run(service.start_web_setup("opencode"))
    assert flow.state == "failed"
    assert flow.error == "opencode_provider_id_required"


def test_status_for_unknown_flow_returns_flow_not_found(service: AgentAuthService) -> None:
    result = service.get_web_flow_status("nonexistent")
    assert result == {"ok": False, "error": "flow_not_found"}


def test_submit_code_unknown_flow(service: AgentAuthService) -> None:
    result = _run(service.submit_web_code("nonexistent", "abc#def"))
    assert result == {"ok": False, "error": "flow_not_found"}


def test_submit_code_rejected_for_codex(service: AgentAuthService) -> None:
    # Codex device-auth never asks for a code; submitting one is a UI bug.
    flow = WebAuthFlow(flow_id="cdx1", backend="codex", state="awaiting_code", awaiting_code=True)
    service._web_flows[flow.flow_id] = flow
    result = _run(service.submit_web_code("cdx1", "abc#def"))
    assert result == {"ok": False, "error": "code_not_supported"}


def test_submit_code_rejected_when_not_awaiting(service: AgentAuthService) -> None:
    flow = WebAuthFlow(flow_id="cl1", backend="claude", state="verifying", awaiting_code=False)
    service._web_flows[flow.flow_id] = flow
    result = _run(service.submit_web_code("cl1", "abc#def"))
    assert result == {"ok": False, "error": "not_awaiting_code"}


def test_submit_code_invalid_format(service: AgentAuthService) -> None:
    flow = WebAuthFlow(
        flow_id="cl2",
        backend="claude",
        state="awaiting_code",
        awaiting_code=True,
        claude_client=object(),  # presence-check only
    )
    service._web_flows[flow.flow_id] = flow

    # Missing separator.
    assert _run(service.submit_web_code("cl2", "no-hash-here")) == {
        "ok": False,
        "error": "invalid_format",
    }
    # Empty left half.
    assert _run(service.submit_web_code("cl2", "#statehere")) == {
        "ok": False,
        "error": "invalid_format",
    }
    # Empty right half.
    assert _run(service.submit_web_code("cl2", "code#")) == {
        "ok": False,
        "error": "invalid_format",
    }


def test_submit_code_happy_path_transitions_to_verifying(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_send = AsyncMock()
    monkeypatch.setattr(service, "_send_claude_callback", mock_send)

    fake_client = object()
    flow = WebAuthFlow(
        flow_id="cl3",
        backend="claude",
        state="awaiting_code",
        awaiting_code=True,
        claude_client=fake_client,
        url="https://claude.ai/oauth/authorize?...",
    )
    service._web_flows[flow.flow_id] = flow

    result = _run(service.submit_web_code("cl3", "  authcode  #  state-token  "))
    assert result == {"ok": True}
    mock_send.assert_awaited_once_with(fake_client, "authcode", "state-token")
    assert flow.state == "verifying"
    assert flow.awaiting_code is False


def test_status_returns_serializable_snapshot(service: AgentAuthService) -> None:
    flow = WebAuthFlow(
        flow_id="cdx2",
        backend="codex",
        state="awaiting_code",
        url="https://auth.openai.com/codex/device",
        device_code="ABCD-EFGH",
        awaiting_code=False,
    )
    service._web_flows[flow.flow_id] = flow
    result = service.get_web_flow_status("cdx2")
    assert result == {
        "ok": True,
        "flow_id": "cdx2",
        "backend": "codex",
        "state": "awaiting_code",
        "url": "https://auth.openai.com/codex/device",
        "device_code": "ABCD-EFGH",
        "awaiting_code": False,
        "error": None,
    }


def test_cancel_unknown_flow(service: AgentAuthService) -> None:
    result = _run(service.cancel_web_flow("nope"))
    assert result == {"ok": False, "error": "flow_not_found"}


def test_cancel_removes_flow_and_marks_state(service: AgentAuthService) -> None:
    flow = WebAuthFlow(flow_id="any", backend="codex", state="awaiting_code")
    service._web_flows[flow.flow_id] = flow
    result = _run(service.cancel_web_flow("any"))
    assert result == {"ok": True}
    assert "any" not in service._web_flows
    assert flow.state == "cancelled"


def test_post_web_success_hook_invocation_when_set(
    service: AgentAuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook fires once after a successful flow; absence is a no-op."""
    calls: list[str] = []

    def hook(backend: str) -> None:
        calls.append(backend)

    persist = AsyncMock()
    monkeypatch.setattr(service, "_persist_backend_auth_mode", persist)
    service._post_web_success_hook = hook
    _run(service._invoke_post_web_success_hook("codex"))
    persist.assert_awaited_once_with("codex", "oauth")
    assert calls == ["codex"]


def test_codex_oauth_success_clears_api_key_state(
    service: AgentAuthService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "sk-old",
                "tokens": {"id_token": "abc"},
            }
        ),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'model_provider = "OpenAI"\n'
        "\n"
        "[model_providers.OpenAI]\n"
        'base_url = "https://relay.example/v1"\n',
        encoding="utf-8",
    )
    saves: list[str] = []
    codex_cfg = SimpleNamespace(
        auth_mode="api_key",
        api_key="sk-old",
        base_url="https://relay.example/v1",
    )
    service.controller.config = SimpleNamespace(
        language="en",
        agents=SimpleNamespace(codex=codex_cfg),
        save=lambda: saves.append("saved"),
    )

    _run(service._invoke_post_web_success_hook("codex"))

    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert auth["auth_mode"] == "chatgpt"
    assert auth["tokens"] == {"id_token": "abc"}
    assert "OPENAI_API_KEY" not in auth
    assert codex_cfg.auth_mode == "oauth"
    assert codex_cfg.api_key is None
    assert codex_cfg.base_url is None
    assert saves == ["saved"]


def test_post_web_success_hook_swallows_exceptions(
    service: AgentAuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misbehaving hook must not surface into the flow waiter."""

    def hook(_backend: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_persist_backend_auth_mode", AsyncMock())
    service._post_web_success_hook = hook
    # Should NOT raise.
    _run(service._invoke_post_web_success_hook("claude"))


def test_claude_oauth_settings_cleanup_failure_fails_web_start(
    service: AgentAuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_cleanup(**_kwargs):
        raise OSError("settings locked")

    service._create_claude_control_client = AsyncMock()
    monkeypatch.setattr("vibe.claude_config.apply_claude_auth", fail_cleanup)

    flow = _run(service.start_web_setup("claude"))

    assert flow.state == "failed"
    assert "Failed to clear Claude Code settings env" in (flow.error or "")
    service._create_claude_control_client.assert_not_awaited()


def test_claude_web_oauth_failures_restore_settings_after_batch_finishes(
    service: AgentAuthService,
) -> None:
    backup = {"ANTHROPIC_API_KEY": "sk-old", "ANTHROPIC_BASE_URL": "https://old.example"}
    service._temporarily_clear_claude_settings_env_for_oauth = AsyncMock(
        side_effect=[backup, None],
    )
    service._restore_claude_settings_env_after_oauth_failure = AsyncMock()

    first_attempt = _run(service._begin_claude_oauth_attempt())
    second_attempt = _run(service._begin_claude_oauth_attempt())

    _run(service._finish_claude_oauth_attempt(first_attempt, succeeded=False))
    service._restore_claude_settings_env_after_oauth_failure.assert_not_awaited()

    _run(service._finish_claude_oauth_attempt(second_attempt, succeeded=False))
    service._restore_claude_settings_env_after_oauth_failure.assert_awaited_once_with(backup)
    assert first_attempt.settings_backup is None
    assert second_attempt.settings_backup is None


def test_stale_claude_web_oauth_failure_ignores_backup_after_newer_success(
    service: AgentAuthService,
) -> None:
    stale_backup = {"ANTHROPIC_API_KEY": "sk-old", "ANTHROPIC_BASE_URL": "https://old.example"}
    service._temporarily_clear_claude_settings_env_for_oauth = AsyncMock(
        side_effect=[stale_backup, None],
    )
    service._restore_claude_settings_env_after_oauth_failure = AsyncMock()

    stale_attempt = _run(service._begin_claude_oauth_attempt())
    newer_attempt = _run(service._begin_claude_oauth_attempt())

    _run(service._finish_claude_oauth_attempt(newer_attempt, succeeded=True))
    _run(service._finish_claude_oauth_attempt(stale_attempt, succeeded=False))

    service._restore_claude_settings_env_after_oauth_failure.assert_not_awaited()
    assert stale_attempt.settings_backup is None
    assert newer_attempt.settings_backup is None


def test_claude_oauth_new_batch_restores_after_previous_batch_success(
    service: AgentAuthService,
) -> None:
    first_backup = {"ANTHROPIC_API_KEY": "sk-old"}
    second_backup = {"ANTHROPIC_API_KEY": "sk-second"}
    service._temporarily_clear_claude_settings_env_for_oauth = AsyncMock(
        side_effect=[first_backup, None, second_backup],
    )
    service._restore_claude_settings_env_after_oauth_failure = AsyncMock()

    stale_attempt = _run(service._begin_claude_oauth_attempt())
    winning_attempt = _run(service._begin_claude_oauth_attempt())
    _run(service._finish_claude_oauth_attempt(winning_attempt, succeeded=True))
    _run(service._finish_claude_oauth_attempt(stale_attempt, succeeded=False))

    next_attempt = _run(service._begin_claude_oauth_attempt())
    _run(service._finish_claude_oauth_attempt(next_attempt, succeeded=False))

    service._restore_claude_settings_env_after_oauth_failure.assert_awaited_once_with(
        second_backup
    )


def test_claude_oauth_begin_persists_backup_before_clearing_settings(
    service: AgentAuthService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    backup = {
        "ANTHROPIC_API_KEY": "sk-old",
        "ANTHROPIC_BASE_URL": "https://relay.example",
    }
    restore_claude_settings_env(backup)

    attempt = _run(service._begin_claude_oauth_attempt())

    assert attempt.settings_backup == backup
    assert read_claude_settings_env() == {}
    settings = json.loads((claude_home / "settings.json").read_text())
    for key, value in MANAGED_ENV_VALUES.items():
        assert settings["env"][key] == value
    assert read_claude_oauth_settings_backup() == backup


def test_claude_oauth_restarts_restore_interrupted_durable_backup(
    service: AgentAuthService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    backup = {
        "ANTHROPIC_API_KEY": "sk-old",
        "ANTHROPIC_BASE_URL": "https://relay.example",
    }
    restore_claude_settings_env(backup)

    _run(service._begin_claude_oauth_attempt())
    assert read_claude_settings_env() == {}
    settings = json.loads((claude_home / "settings.json").read_text())
    for key, value in MANAGED_ENV_VALUES.items():
        assert settings["env"][key] == value

    AgentAuthService(_StubController())

    assert read_claude_settings_env() == backup
    settings = json.loads((claude_home / "settings.json").read_text())
    for key, value in MANAGED_ENV_VALUES.items():
        assert settings["env"][key] == value
    assert read_claude_oauth_settings_backup() is None


def test_claude_oauth_restarts_restore_pending_backup_even_when_config_is_oauth(
    service: AgentAuthService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibe.claude_config import write_claude_oauth_settings_backup

    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setattr(_Backend, "auth_mode", "oauth", raising=False)
    monkeypatch.setattr(_Backend, "auth_mode_set", True, raising=False)
    backup = {
        "ANTHROPIC_API_KEY": "sk-old",
        "ANTHROPIC_BASE_URL": "https://relay.example",
    }
    write_claude_oauth_settings_backup(backup)

    service._recover_interrupted_claude_oauth_settings_backup()

    assert read_claude_settings_env() == backup
    assert read_claude_oauth_settings_backup() is None


def test_claude_oauth_rollback_serializes_before_retry(
    service: AgentAuthService,
) -> None:
    first_backup = {"ANTHROPIC_API_KEY": "sk-old"}
    retry_backup = {"ANTHROPIC_API_KEY": "sk-retry"}
    service._temporarily_clear_claude_settings_env_for_oauth = AsyncMock(
        side_effect=[first_backup, retry_backup],
    )
    service._clear_pending_claude_oauth_settings_backup = AsyncMock()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    order: list[str] = []

    async def restore(_backup):
        order.append("restore-start")
        restore_started.set()
        await release_restore.wait()
        order.append("restore-end")
        return True

    service._restore_claude_settings_env_after_oauth_failure = restore

    async def scenario():
        first_attempt = await service._begin_claude_oauth_attempt()
        finish_task = asyncio.create_task(
            service._finish_claude_oauth_attempt(first_attempt, succeeded=False)
        )
        await restore_started.wait()
        retry_task = asyncio.create_task(service._begin_claude_oauth_attempt())
        await asyncio.sleep(0)

        assert not retry_task.done()
        assert service._temporarily_clear_claude_settings_env_for_oauth.await_count == 1

        release_restore.set()
        retry_attempt = await retry_task
        await finish_task
        return retry_attempt

    retry_attempt = _run(scenario())

    assert order == ["restore-start", "restore-end"]
    assert retry_attempt.settings_backup == retry_backup


def test_post_web_success_hook_unset_is_safe(
    service: AgentAuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "_persist_backend_auth_mode", AsyncMock())
    service._post_web_success_hook = None
    _run(service._invoke_post_web_success_hook("claude"))


# ---------------------------------------------------------------------------
# OpenCode per-provider OAuth web flow
# ---------------------------------------------------------------------------


class _FakeOpencodeServer:
    """In-memory stub of ``OpenCodeServer`` for the OAuth path.

    ``start_provider_oauth`` returns the authorize-stub the test sets up
    via ``next_authorize``; ``wait_provider_oauth`` blocks on a future
    the test resolves manually so we can drive completion deterministically.
    """

    def __init__(self) -> None:
        self.next_authorize: dict = {}
        self.auth_map: dict = {}
        self.wait_future: asyncio.Future = asyncio.get_event_loop_policy().new_event_loop().create_future()
        self.start_calls: list[tuple[str, int, dict]] = []
        self.wait_calls: list[tuple[str, int, dict]] = []
        self.catalog: dict = {}
        self.created_session: dict = {"info": {"id": "sess_probe"}}
        self.messages: list[dict] = []
        self.prompt_calls: list[dict] = []
        self.abort_calls: list[tuple[str, str]] = []
        self.active_calls: list[str] = []
        self.inactive_calls: list[str] = []
        self.message_sent = False

    async def get_provider_auth(self):
        return self.auth_map

    async def get_available_models(self, _directory):
        return self.catalog

    async def create_session(self, _directory, *, title):
        return self.created_session

    async def list_messages(self, _session_id, _directory):
        return self.messages if self.message_sent else []

    async def prompt_async(self, **kwargs):
        self.prompt_calls.append(kwargs)
        self.message_sent = True

    async def mark_run_active(self, session_id):
        self.active_calls.append(session_id)

    async def mark_run_inactive(self, session_id):
        self.inactive_calls.append(session_id)

    async def abort_session(self, session_id, directory):
        self.abort_calls.append((session_id, directory))

    async def get_recent_session_error(self, session_id, since=None):
        return None

    async def get_provider_api_diagnostic(self, provider_id, model_id):
        return None

    async def start_provider_oauth(self, provider_id, *, method, prompt_answers):
        self.start_calls.append((provider_id, method, prompt_answers))
        return self.next_authorize

    async def wait_provider_oauth(self, provider_id, *, method, prompt_answers, timeout):
        self.wait_calls.append((provider_id, method, prompt_answers))
        return await self.wait_future


def test_start_web_setup_opencode_extracts_url_and_device_code(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeOpencodeServer()
    fake.auth_map = {
        "openai": [
            {"type": "oauth", "label": "ChatGPT Pro/Plus (browser)"},
            {"type": "oauth", "label": "ChatGPT Pro/Plus (headless)"},
            {"type": "api", "label": "Manually enter API Key"},
        ]
    }
    fake.next_authorize = {
        "url": "https://auth.openai.com/codex/device",
        "instructions": "Enter code: YR8I-QJJUH",
    }
    monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=fake))

    flow = _run(_start_opencode_flow_without_waiter(service, "openai"))

    # Headless variant (index 1) wins because the resolver walks the
    # auth list in reverse — important for remote sessions where the
    # localhost-callback "browser" variant (index 0) can't complete.
    assert fake.start_calls == [("openai", 1, {})]
    assert flow.state == "awaiting_code"
    assert flow.url == "https://auth.openai.com/codex/device"
    assert flow.device_code == "YR8I-QJJUH"
    # OpenCode device flow auto-completes; UI must not show a code-submit input.
    assert flow.awaiting_code is False


def test_start_web_setup_opencode_github_copilot_passes_prompt_answer(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """github-copilot's first method has a ``deploymentType`` prompt; the
    resolver pre-fills ``github.com`` so the user doesn't have to pick
    enterprise vs public on first sign-in."""
    fake = _FakeOpencodeServer()
    fake.auth_map = {
        "github-copilot": [
            {"type": "oauth", "label": "Login with GitHub Copilot"},
        ]
    }
    fake.next_authorize = {
        "url": "https://github.com/login/device",
        "instructions": "Enter code: 335B-09BE",
    }
    monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=fake))

    _run(_start_opencode_flow_without_waiter(service, "github-copilot"))

    assert fake.start_calls == [
        ("github-copilot", 0, {"deploymentType": "github.com"}),
    ]


def test_start_web_setup_opencode_url_only_flow_has_no_device_code(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Browser-redirect flows (gitlab, poe) return ``url`` only — no
    "Enter code: XXX" line. ``device_code`` must stay ``None`` so the UI
    skips the device-code block."""
    fake = _FakeOpencodeServer()
    fake.auth_map = {"gitlab": [{"type": "oauth", "label": "GitLab OAuth"}]}
    fake.next_authorize = {
        "url": "https://gitlab.com/oauth/authorize?...",
        "instructions": "Your browser will open for authentication.",
    }
    monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=fake))

    flow = _run(_start_opencode_flow_without_waiter(service, "gitlab"))
    assert flow.state == "awaiting_code"
    assert flow.url is not None
    assert flow.device_code is None


def test_start_web_setup_opencode_surfaces_server_failure(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the OpenCode daemon isn't reachable, the flow lands in
    ``failed`` with a typed error string so the UI can render an
    actionable sentence rather than ``cli_failed``."""
    monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=None))
    flow = _run(service.start_web_setup("opencode", provider_id="openai"))
    assert flow.state == "failed"
    assert flow.error == "opencode_server_unavailable"


def test_opencode_oauth_success_clears_provider_options_key(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeOpencodeServer()
    fake.auth_map = {"openai": [{"type": "oauth", "label": "ChatGPT Pro/Plus"}]}
    fake.next_authorize = {
        "url": "https://auth.openai.com/codex/device",
        "instructions": "Enter code: YR8I-QJJUH",
    }
    fake.wait_provider_oauth = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=fake))
    clear_key = AsyncMock()
    monkeypatch.setattr(service, "_clear_opencode_provider_options_key_for_oauth", clear_key)
    hook_calls: list[str] = []
    service._post_web_success_hook = lambda b: hook_calls.append(b)

    async def run_flow():
        flow = await service.start_web_setup("opencode", provider_id="openai")
        await flow.waiter_task
        return flow

    flow = _run(run_flow())

    clear_key.assert_awaited_once_with("openai")
    assert hook_calls == ["opencode"]
    assert flow.state == "success"


def test_opencode_provider_test_returns_excerpt_from_non_text_part(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeOpencodeServer()
    fake.catalog = {
        "providers": [
            {
                "id": "anthropic",
                "models": {"claude-opus-4.8": {}},
            }
        ]
    }
    fake.messages = [
        {
            "info": {
                "id": "msg_assistant",
                "role": "assistant",
                "time": {"completed": 123},
            },
            "parts": [
                {
                    "type": "reasoning",
                    "id": "part_reasoning",
                    "text": "Hello from a non-text OpenCode part",
                }
            ],
        }
    ]
    monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=fake))

    result = _run(service.test_opencode_provider("anthropic"))

    assert result["ok"] is True
    assert result["model"] == "claude-opus-4.8"
    assert result["excerpt"] == "Hello from a non-text OpenCode part"
    assert fake.prompt_calls[-1]["model"] == {
        "providerID": "anthropic",
        "modelID": "claude-opus-4.8",
    }
    assert fake.active_calls == ["sess_probe"]
    assert fake.abort_calls == [("sess_probe", os.path.expanduser("~"))]
    assert fake.inactive_calls == ["sess_probe"]


def test_opencode_provider_test_fails_on_empty_terminal_message(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeOpencodeServer()
    fake.catalog = {
        "providers": [
            {
                "id": "glm",
                "models": {
                    "glm-5.2": {
                        "capabilities": {"reasoning": False},
                    }
                },
            }
        ]
    }
    fake.messages = [
        {
            "info": {
                "id": "msg_assistant",
                "role": "assistant",
                "time": {"completed": 123},
                "finish": "unknown",
                "tokens": {
                    "input": 8,
                    "output": 4,
                    "reasoning": 2,
                    "cache": {"read": 1, "write": 0},
                },
            },
            "parts": [
                {"type": "step-start", "id": "step_start"},
                {"type": "step-finish", "id": "step_finish"},
            ],
        }
    ]
    monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=fake))

    result = _run(service.test_opencode_provider("glm", model="glm-5.2"))

    assert result["ok"] is False
    assert result["error"] == "empty_response"
    assert result["model"] == "glm-5.2"
    assert "glm-5.2" in result["detail"]
    assert fake.prompt_calls[-1]["reasoning_effort"] is None
    assert fake.active_calls == ["sess_probe"]
    assert fake.abort_calls == [("sess_probe", os.path.expanduser("~"))]
    assert fake.inactive_calls == ["sess_probe"]


def test_opencode_provider_test_uses_catalog_model_casing(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeOpencodeServer()
    fake.catalog = {
        "providers": [
            {
                "id": "glm",
                "models": {
                    "glm-5.2": {"id": "glm-5.2"},
                },
            }
        ]
    }
    fake.messages = [
        {
            "info": {
                "id": "msg_assistant",
                "role": "assistant",
                "time": {"completed": 123},
                "finish": "stop",
            },
            "parts": [{"type": "text", "text": "OK"}],
        }
    ]
    monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=fake))

    result = _run(service.test_opencode_provider("glm", model="GLM-5.2"))

    assert result["ok"] is True
    assert result["model"] == "glm-5.2"
    assert fake.prompt_calls[-1]["model"] == {"providerID": "glm", "modelID": "glm-5.2"}


def test_remove_web_auth_rejects_unsupported_backend(service: AgentAuthService) -> None:
    # OpenCode joined ``WEB_BACKENDS`` for OAuth, but ``remove_web_auth``
    # is claude / codex specific — opencode providers use the
    # per-provider DELETE endpoint instead. Both ``opencode`` and any
    # other name are rejected here.
    assert _run(service.remove_web_auth("opencode")) == {"ok": False, "error": "unsupported_backend"}
    assert _run(service.remove_web_auth("gemini")) == {"ok": False, "error": "unsupported_backend"}


def test_remove_web_auth_runs_logout_and_returns_ok(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``_run_utility_command`` now returns ``(ok, error_excerpt)`` so
    # ``remove_web_auth`` can surface a partial failure when ``codex
    # logout`` / ``claude auth logout`` exits non-zero. The success
    # path mocks must yield ``(True, None)``.
    run_cmd = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(service, "_run_utility_command", run_cmd)
    monkeypatch.setattr("vibe.claude_config.apply_claude_auth", lambda **_kwargs: None)
    hook_calls: list[str] = []
    service._post_web_success_hook = lambda b: hook_calls.append(b)

    result = _run(service.remove_web_auth("claude"))
    assert result == {"ok": True}
    # Claude logout subcommand is ``claude auth logout``.
    run_cmd.assert_awaited_once()
    args = run_cmd.call_args.args
    assert "auth" in args and "logout" in args
    # Hook fires so the live controller can refresh.
    assert hook_calls == ["claude"]


def test_remove_web_auth_codex_uses_logout_subcommand(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_cmd = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(service, "_run_utility_command", run_cmd)
    result = _run(service.remove_web_auth("codex"))
    assert result == {"ok": True}
    # Codex uses just ``codex logout`` (no nested ``auth`` subcommand).
    args = run_cmd.call_args.args
    assert "logout" in args and "auth" not in args


def test_remove_web_auth_surfaces_logout_failure(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex P2: a failed ``logout`` previously got swallowed and the
    # API returned ``ok: true``, misleading the UI into showing a
    # green sign-out toast while the backend creds remained intact.
    # Now the failure rides back as ``partial`` + ``warning`` so the
    # frontend can show a warning toast and the on-disk state can be
    # cleaned up manually.
    run_cmd = AsyncMock(return_value=(False, "exit 1: not logged in"))
    monkeypatch.setattr(service, "_run_utility_command", run_cmd)
    monkeypatch.setattr("vibe.claude_config.apply_claude_auth", lambda **_kwargs: None)
    result = _run(service.remove_web_auth("claude"))
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["warning"] == "logout_failed"
    assert "exit 1" in result["detail"]


def test_remove_web_auth_surfaces_claude_settings_cleanup_failure(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_cmd = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(service, "_run_utility_command", run_cmd)

    def fail_cleanup(**_kwargs):
        raise OSError("settings locked")

    monkeypatch.setattr("vibe.claude_config.apply_claude_auth", fail_cleanup)

    result = _run(service.remove_web_auth("claude"))

    assert result["ok"] is True
    assert result["partial"] is True
    assert result["warning"] == "settings_cleanup_failed"
    assert "Failed to clear Claude Code settings env" in result["detail"]


def test_clear_claude_oauth_for_api_key_mode_restores_key_settings(
    service: AgentAuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore_claude_settings_env(
        {
            "ANTHROPIC_API_KEY": "sk-active",
            "ANTHROPIC_BASE_URL": "https://relay.example.invalid",
        }
    )
    credentials_path = get_claude_credentials_path()
    credentials_path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "stale-oauth",
                    "refreshToken": "stale-refresh",
                }
            }
        ),
        encoding="utf-8",
    )
    run_cmd = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(service, "_run_utility_command", run_cmd)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-shell-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-shell-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://shell.example.invalid")

    result = _run(service.clear_claude_oauth_for_api_key_mode())

    assert result == {"ok": True}
    run_cmd.assert_awaited_once()
    args = run_cmd.call_args.args
    kwargs = run_cmd.call_args.kwargs
    assert "auth" in args and "logout" in args
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in kwargs["env"]
    assert "ANTHROPIC_BASE_URL" not in kwargs["env"]
    assert read_claude_settings_env() == {
        "ANTHROPIC_API_KEY": "sk-active",
        "ANTHROPIC_BASE_URL": "https://relay.example.invalid",
    }
    assert not credentials_path.exists()


def test_clear_claude_oauth_credentials_only_preserves_api_key_config(
    service: AgentAuthService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore_claude_settings_env(
        {
            "ANTHROPIC_API_KEY": "sk-active",
            "ANTHROPIC_BASE_URL": "https://relay.example.invalid",
        }
    )
    run_cmd = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(service, "_run_utility_command", run_cmd)

    result = _run(service.clear_claude_oauth_credentials_only())

    assert result == {"ok": True}
    assert read_claude_settings_env() == {
        "ANTHROPIC_API_KEY": "sk-active",
        "ANTHROPIC_BASE_URL": "https://relay.example.invalid",
    }


def test_clear_claude_oauth_credentials_files_removes_known_token_files(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    current = claude_home / ".credentials.json"
    legacy = claude_home / "credentials.json"
    current.write_text("{}", encoding="utf-8")
    legacy.write_text("{}", encoding="utf-8")

    removed = clear_claude_oauth_credentials_files(tmp_path)

    assert str(current) in removed
    assert str(legacy) in removed
    assert not current.exists()
    assert not legacy.exists()


def test_test_web_auth_rejects_unsupported_backend(service: AgentAuthService) -> None:
    # OpenCode joins ``WEB_BACKENDS`` for OAuth start; ``test_web_auth``
    # still rejects it (probe is run by the OpenCode daemon itself).
    assert _run(service.test_web_auth("opencode")) == {"ok": False, "error": "unsupported_backend"}
    assert _run(service.test_web_auth("gemini")) == {"ok": False, "error": "unsupported_backend"}


def test_test_web_auth_surfaces_cli_not_found(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _spawn(*_args, **_kwargs):
        raise FileNotFoundError("no such cli")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    result = _run(service.test_web_auth("codex"))
    assert result["ok"] is False
    assert result["error"] == "cli_not_found"


def test_test_web_auth_happy_path_returns_excerpt(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing probe surfaces the first non-blank stdout line + duration."""

    class _FakeProcess:
        returncode = 0

        async def communicate(self):
            return (b"\nHello from the model\nmore text", b"")

    async def _spawn(*_args, **_kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    result = _run(service.test_web_auth("codex"))
    assert result["ok"] is True
    assert result["excerpt"] == "Hello from the model"
    assert isinstance(result["duration_ms"], int)


def test_verify_web_login_claude_forces_oauth_env(
    service: AgentAuthService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After Claude's OAuth waiter completes, the verification probe must
    ignore stale inherited Anthropic env vars or it can report "no login"
    against the wrong auth source.
    """

    captured: dict = {}
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text(
        '{"env":{"ANTHROPIC_API_KEY":"sk-settings","ANTHROPIC_BASE_URL":"https://settings-relay.example"}}',
        encoding="utf-8",
    )

    class _FakeProcess:
        returncode = 0

        async def communicate(self):
            return (b'{"loggedIn": true}', b"")

    async def _spawn(*_args, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return _FakeProcess()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stale-shell")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://stale-relay.example")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setattr(_Backend, "auth_mode", "api_key", raising=False)
    monkeypatch.setattr(_Backend, "auth_mode_set", True, raising=False)
    monkeypatch.setattr(_Backend, "api_key", "sk-configured", raising=False)
    monkeypatch.setattr(_Backend, "base_url", "https://configured-relay.example", raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    original_backup = {"ANTHROPIC_API_KEY": "sk-original"}
    attempt = ClaudeOAuthAttempt(
        attempt_id=1,
        batch=ClaudeOAuthBatch(backup=original_backup),
    )
    clear_calls: list[str] = []
    observed_backup_after_probe: list[dict[str, str] | None] = []

    async def clear_settings():
        clear_calls.append("called")
        return {"ANTHROPIC_API_KEY": "sk-restored-by-overlap"}

    original_verify_login = service._verify_login

    async def verify_login_with_existing_backup(flow):
        result = await original_verify_login(flow)
        observed_backup_after_probe.append(flow.claude_oauth_attempt.settings_backup)
        return result

    monkeypatch.setattr(service, "_verify_login", verify_login_with_existing_backup)
    monkeypatch.setattr(service, "_temporarily_clear_claude_settings_env_for_oauth", clear_settings)

    ok, detail = _run(
        service._verify_web_login(
            "claude",
            force_oauth=True,
            claude_oauth_attempt=attempt,
        )
    )

    assert ok is True
    assert detail == '{"loggedIn": true}'
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "ANTHROPIC_BASE_URL" not in captured["env"]
    assert clear_calls == ["called"]
    assert observed_backup_after_probe == [original_backup]
    assert attempt.settings_backup == original_backup


def test_test_web_auth_claude_oauth_env_removes_stale_anthropic_vars(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Settings Test button runs a real Claude subprocess; when OAuth is
    explicitly selected it must use the same env cleanup as live sessions.
    """

    captured: dict = {}

    class _FakeProcess:
        returncode = 0

        async def communicate(self):
            return (b"Hello from Claude", b"")

    async def _spawn(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env") or {}
        return _FakeProcess()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stale-shell")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-stale-shell")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://stale-relay.example")
    monkeypatch.setattr(_Backend, "auth_mode", "oauth", raising=False)
    monkeypatch.setattr(_Backend, "auth_mode_set", True, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    result = _run(service.test_web_auth("claude"))

    assert result["ok"] is True
    assert captured["args"][:2] == ("/usr/bin/echo", "-p")
    assert "--bare" not in captured["args"]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]
    assert "ANTHROPIC_BASE_URL" not in captured["env"]


def test_test_web_auth_claude_runs_in_runtime_cwd(
    service: AgentAuthService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain Claude print mode reads project config from cwd; the Settings
    probe must use the same runtime cwd as live Agent turns.
    """

    runtime_cwd = tmp_path / "agent-workdir"
    captured: dict = {}

    class _Runtime:
        default_cwd = str(runtime_cwd)

    class _FakeProcess:
        returncode = 0

        async def communicate(self):
            return (b"Hello from Claude", b"")

    async def _spawn(*_args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProcess()

    monkeypatch.setattr(_Config, "runtime", _Runtime(), raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    result = _run(service.test_web_auth("claude"))

    assert result["ok"] is True
    assert captured["cwd"] == str(runtime_cwd)
    assert runtime_cwd.is_dir()


def test_test_web_auth_claude_oauth_reports_settings_cleanup_failure(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _spawn(*_args, **_kwargs):
        raise AssertionError("Claude probe must not spawn when cleanup fails")

    def fail_cleanup(**_kwargs):
        raise OSError("settings locked")

    monkeypatch.setattr(_Backend, "auth_mode", "oauth", raising=False)
    monkeypatch.setattr(_Backend, "auth_mode_set", True, raising=False)
    monkeypatch.setattr("vibe.claude_config.apply_claude_auth", fail_cleanup)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    result = _run(service.test_web_auth("claude"))

    assert result["ok"] is False
    assert result["error"] == "settings_cleanup_failed"
    assert "Failed to clear Claude Code settings env" in result["detail"]


def test_test_web_auth_failure_surfaces_stderr(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeProcess:
        returncode = 7

        async def communicate(self):
            return (b"", b"Authentication failed: no credentials configured")

    async def _spawn(*_args, **_kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    result = _run(service.test_web_auth("claude"))
    assert result["ok"] is False
    # The classifier turns "Authentication failed" stderr into the
    # specific ``invalid_credentials`` code so the UI can render the
    # actionable "Replace your API key or re-authenticate" sentence.
    assert result["error"] == "invalid_credentials"
    assert result["exit_code"] == 7
    assert "Authentication failed" in (result.get("detail") or "")


def test_test_web_auth_not_logged_in_has_specific_error_code(
    service: AgentAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeProcess:
        returncode = 1

        async def communicate(self):
            return (b"Not logged in \xc2\xb7 Please run /login", b"")

    async def _spawn(*_args, **_kwargs):
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)

    result = _run(service.test_web_auth("claude"))

    assert result["ok"] is False
    assert result["error"] == "not_logged_in"
    assert result["exit_code"] == 1
    assert "Not logged in" in (result.get("detail") or "")
