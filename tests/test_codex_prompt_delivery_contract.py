"""Native prompt contract; opt in with CODEX_PROMPT_CONTRACT_BINARY.

Uses an isolated Codex home and a loopback Responses server, never credentials
or a real model. The catalog deliberately owns the default collaboration text.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from aiohttp import web
import pytest

from modules.agents.codex.agent import CodexAgent
from modules.agents.codex.transport import CodexTransport


BINARY = os.environ.get("CODEX_PROMPT_CONTRACT_BINARY")
pytestmark = pytest.mark.skipif(not BINARY, reason="requires an explicitly selected Codex binary")
MODEL = "avibe-prompt-contract"
CATALOG_PROMPT = "CATALOG_OWNS_DEFAULT_COLLABORATION_MODE"
PROMPT = "\n\n".join(
    (Path(__file__).resolve().parents[1] / "core" / "prompts" / name).read_text()
    for name in ("quick-replies.md", "session-title.md")
)


def _agent(marker):
    agent = object.__new__(CodexAgent)
    agent.controller = SimpleNamespace(get_codex_overrides=Mock(return_value=(None, MODEL, "high")))
    agent.codex_config = SimpleNamespace(default_model=None)

    def persist(*_args, **kwargs):
        marker.clear()
        marker.update(kwargs["value"])
        return True

    agent.sessions = SimpleNamespace(
        get_agent_session_runtime_marker=lambda *_args, **_kwargs: dict(marker) or None,
        set_agent_session_runtime_marker=persist,
    )
    agent.ensure_agent_session_id = Mock(return_value="contract-session")
    agent._build_input = Mock(return_value=[{"type": "text", "text": "Hello", "text_elements": []}])
    agent._write_caller_env_script = Mock()
    agent._turn_registry = SimpleNamespace(
        begin_turn_start=Mock(),
        get_bootstrapped_turn_id=Mock(return_value=None),
        finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
    )
    return agent


def _developer_texts(body):
    return [
        content["text"]
        for item in body["input"]
        if item.get("role") == "developer"
        for content in item["content"]
        if content.get("type") == "input_text"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["new-thread", "legacy-collaboration"])
async def test_native_model_receives_prompt_once_across_turns_and_restart(tmp_path, legacy):
    requests = []
    completed = asyncio.Queue()

    async def responses(request):
        requests.append(await request.json())
        response_id = f"resp-{len(requests)}"
        compacting = any(item.get("type") == "compaction_trigger" for item in requests[-1]["input"])
        events = [
            {"type": "response.created", "response": {"id": response_id}},
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": f"msg-{len(requests)}",
                    "content": [{"type": "output_text", "text": "OK", "annotations": []}],
                },
            },
            {
                "type": "response.completed",
                "response": {"id": response_id, "usage": {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}},
            },
        ]
        if compacting:
            events[1]["item"] = {"type": "compaction", "encrypted_content": "CONTRACT_SUMMARY"}
        return web.Response(
            text="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/responses", responses)
    runner = web.AppRunner(app)
    await runner.setup()
    server = await asyncio.get_running_loop().create_server(runner.server, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": MODEL,
                        "display_name": MODEL,
                        "supported_reasoning_levels": [],
                        "shell_type": "unified_exec",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 1,
                        "support_verbosity": False,
                        "experimental_supported_tools": [],
                        "truncation_policy": {"mode": "tokens", "limit": 10000},
                        "base_instructions": "You are a test assistant.",
                        "model_messages": {"collaboration_modes": {"default": CATALOG_PROMPT}},
                    }
                ]
            }
        )
    )
    (codex_home / "config.toml").write_text(
        f'model = "{MODEL}"\nmodel_provider = "contract"\n'
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
        '[model_providers.contract]\nname = "OpenAI"\nwire_api = "responses"\n'
        f'base_url = "http://127.0.0.1:{port}"\nrequires_openai_auth = false\n'
    )

    def transport():
        return CodexTransport(
            binary=BINARY,
            cwd=str(tmp_path),
            runtime_env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "CODEX_HOME": str(codex_home),
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
            },
        )

    async def notification(method, params):
        if method == "turn/completed":
            await completed.put(params)

    async def finish_turn():
        event = await asyncio.wait_for(completed.get(), timeout=30)
        assert event["turn"]["status"] == "completed", event

    native = transport()
    native.on_notification(notification)
    try:
        await native.start()
        thread = await native.send_request("thread/start", {"cwd": str(tmp_path), "model": MODEL})
        thread_id = thread["thread"]["id"]
        marker = {}
        if legacy:
            await native.send_request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": "Hello", "text_elements": []}],
                    "collaborationMode": {
                        "mode": "default",
                        "settings": {"model": MODEL, "reasoning_effort": "high", "developer_instructions": PROMPT},
                    },
                },
            )
            await finish_turn()
            # Characterize the upstream precedence that broke the old route.
            assert any(CATALOG_PROMPT in text for text in _developer_texts(requests[-1]))
            assert PROMPT not in _developer_texts(requests[-1])
            marker.update(thread_id=thread_id, strategy="collaboration", sha256=CodexAgent._prompt_fingerprint(PROMPT))

        request = SimpleNamespace(
            session_key="contract",
            base_session_id="contract",
            composite_session_id="avibe:contract",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        agent = _agent(marker)
        for _ in range(2):
            await agent._start_turn(native, request, thread_id, developer_instructions=PROMPT)
            await finish_turn()
            assert _developer_texts(requests[-1]).count(PROMPT) == 1
            assert requests[-1]["model"] == MODEL
            assert requests[-1]["reasoning"]["effort"] == "high"

        await native.stop()
        native = transport()
        native.on_notification(notification)
        await native.start()
        await native.send_request("thread/resume", {"threadId": thread_id})
        agent = _agent(marker)
        await agent._start_turn(native, request, thread_id, developer_instructions=PROMPT)
        await finish_turn()
        assert _developer_texts(requests[-1]).count(PROMPT) == 1

        changed = PROMPT + "\nUPDATED_SESSION_RULE"
        await agent._start_turn(native, request, thread_id, developer_instructions=changed)
        await finish_turn()
        assert _developer_texts(requests[-1]).count(changed) == 1

        await native.send_request("thread/compact/start", {"threadId": thread_id})
        await finish_turn()
        assert any(item.get("type") == "compaction_trigger" for item in requests[-1]["input"])
        await agent._start_turn(native, request, thread_id, developer_instructions=changed)
        await finish_turn()
        assert _developer_texts(requests[-1]).count(changed) == 1
    finally:
        await native.stop()
        server.close()
        await server.wait_closed()
        await runner.cleanup()
