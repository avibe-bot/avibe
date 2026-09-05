import asyncio
import builtins
import importlib.util
import json
import stat
from pathlib import Path

import pytest

import modules.claude_sdk_compat as compat


requires_claude_sdk = pytest.mark.skipif(
    not compat.CLAUDE_SDK_AVAILABLE,
    reason="claude_agent_sdk is not installed",
)


@requires_claude_sdk
@pytest.mark.asyncio
async def test_launch_env_stays_out_of_argv_and_is_cleaned_after_disconnect(monkeypatch):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    token = "private-launch-settings-token"
    payload = {"autoMemoryEnabled": False, "env": {"ANTHROPIC_AUTH_TOKEN": token}}
    options = compat.ClaudeAgentOptions(
        settings=json.dumps(payload), sandbox={"enabled": False}, cli_path="/fixture/claude",
    )
    files = []

    async def connect(client, prompt=None):
        path = Path(client.options.settings)
        files.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert json.loads(path.read_text()) == {**payload, "sandbox": {"enabled": False}}
        transport = SubprocessCLITransport(prompt="", options=client.options)
        argv = transport._build_command()
        assert argv[argv.index("--settings") + 1] == str(path)
        assert token not in " ".join(argv)
        assert options.settings == json.dumps(payload)
        assert options.sandbox == {"enabled": False}

    async def disconnect(client):
        assert Path(client.options.settings).exists()

    monkeypatch.setattr(compat._ClaudeSDKClient, "connect", connect)
    monkeypatch.setattr(compat._ClaudeSDKClient, "disconnect", disconnect)
    client = compat.ClaudeSDKClient(options=options)
    for _ in range(2):
        await client.connect()
        assert files[-1].exists()
        await client.disconnect()
        assert not files[-1].parent.exists()
        assert client.options is options
    assert files[0] != files[1]


@requires_claude_sdk
@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("stage", ["connect", "disconnect"])
async def test_launch_settings_cleanup_covers_failure_and_cancellation(monkeypatch, error, stage):
    options = compat.ClaudeAgentOptions(settings='{"env":{"ANTHROPIC_AUTH_TOKEN":"fixture"}}')
    files = []

    async def connect(client, prompt=None):
        files.append(Path(client.options.settings))
        if stage == "connect":
            raise error("connect interrupted")

    async def disconnect(client):
        raise error("disconnect interrupted")

    monkeypatch.setattr(compat._ClaudeSDKClient, "connect", connect)
    monkeypatch.setattr(compat._ClaudeSDKClient, "disconnect", disconnect)
    client = compat.ClaudeSDKClient(options=options)
    with pytest.raises(error):
        await client.connect()
        await client.disconnect()
    assert not files[0].parent.exists()
    assert client.options is options


@requires_claude_sdk
@pytest.mark.asyncio
async def test_launch_settings_preserve_native_options_without_env(monkeypatch):
    options = compat.ClaudeAgentOptions(settings='{"autoMemoryEnabled":false}', sandbox={"enabled": False})

    async def connect(client, prompt=None):
        assert client.options is options

    monkeypatch.setattr(compat._ClaudeSDKClient, "connect", connect)
    await compat.ClaudeSDKClient(options=options).connect()


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


def test_older_sdk_without_task_message_classes_stays_available(monkeypatch):
    original_import = builtins.__import__
    task_types = {
        "TaskNotificationMessage",
        "TaskProgressMessage",
        "TaskStartedMessage",
    }

    def _hide_task_types(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "claude_agent_sdk" and task_types.intersection(fromlist or ()):
            raise ImportError("older SDK has no task message classes")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _hide_task_types)
    module_path = Path(__file__).resolve().parents[1] / "modules" / "claude_sdk_compat.py"
    spec = importlib.util.spec_from_file_location("claude_sdk_compat_older", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert module.CLAUDE_SDK_AVAILABLE is True
    assert issubclass(module.TaskStartedMessage, module.SystemMessage)
    assert issubclass(module.TaskProgressMessage, module.SystemMessage)
    assert issubclass(module.TaskNotificationMessage, module.SystemMessage)
