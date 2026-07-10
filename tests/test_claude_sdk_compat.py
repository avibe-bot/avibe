import asyncio
import builtins
import importlib.util
from pathlib import Path

import pytest

import modules.claude_sdk_compat as compat


requires_claude_sdk = pytest.mark.skipif(
    not compat.CLAUDE_SDK_AVAILABLE,
    reason="claude_agent_sdk is not installed",
)


class _FakeQuery:
    def __init__(self, messages):
        self._messages = messages

    async def receive_messages(self):
        for message in self._messages:
            yield message


async def _collect_messages(messages):
    client = compat.ClaudeSDKClient()
    client._query = _FakeQuery(messages)
    return [message async for message in client.receive_messages()]


@requires_claude_sdk
def test_receive_messages_skips_rate_limit_event():
    messages = asyncio.run(
        _collect_messages(
            [
                {"type": "rate_limit_event", "retry_after_ms": 1000},
                {"type": "system", "subtype": "init", "cwd": "/tmp"},
            ]
        )
    )

    assert len(messages) == 1
    assert isinstance(messages[0], compat.SystemMessage)
    assert messages[0].subtype == "init"


@requires_claude_sdk
def test_receive_messages_skips_unknown_types_returning_none():
    messages = asyncio.run(_collect_messages([{"type": "mystery_event"}]))

    assert messages == []


@requires_claude_sdk
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "type": "system",
                "subtype": "model_refusal_fallback",
                "trigger": "refusal",
                "direction": "retry",
                "original_model": "claude-fable-5",
                "fallback_model": "claude-opus-4-8",
                "request_id": "req_test",
                "api_refusal_category": "cyber",
                "content": "Fable 5 safeguards flagged this request. Switched to Opus 4.8.",
                "uuid": "msg_test",
                "session_id": "session_test",
            },
            id="sdk",
        ),
        pytest.param(
            {
                "type": "system",
                "subtype": "model_refusal_fallback",
                "level": "warning",
                "trigger": "refusal",
                "originalModel": "claude-fable-5[1m]",
                "fallbackModel": "claude-opus-4-8",
                "apiRefusalCategory": None,
                "apiRefusalExplanation": None,
            },
            id="legacy-transcript",
        ),
    ],
)
def test_receive_messages_preserves_model_refusal_fallback_payload(payload):
    messages = asyncio.run(_collect_messages([payload]))

    assert len(messages) == 1
    assert isinstance(messages[0], compat.SystemMessage)
    assert messages[0].subtype == "model_refusal_fallback"
    assert messages[0].data == payload


def test_missing_sdk_permission_allow_fallback_is_non_throwing(monkeypatch):
    original_import = builtins.__import__

    def _block_claude_sdk(name, *args, **kwargs):
        if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
            raise ModuleNotFoundError("claude_agent_sdk")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_claude_sdk)
    module_path = Path(__file__).resolve().parents[1] / "modules" / "claude_sdk_compat.py"
    spec = importlib.util.spec_from_file_location("claude_sdk_compat_missing", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    result = module.PermissionResultAllow()

    assert module.CLAUDE_SDK_AVAILABLE is False
    assert result.behavior == "allow"
    assert result.updated_input is None
    assert result.updated_permissions is None
