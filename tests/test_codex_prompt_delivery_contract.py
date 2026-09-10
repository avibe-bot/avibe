"""Native prompt contract; opt in with CODEX_PROMPT_CONTRACT_BINARY.

Uses an isolated Codex home and a loopback Responses server, never credentials
or a real model. The catalog deliberately owns the default collaboration text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
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
        get_agent_session_id=lambda *_args: marker.get("thread_id"),
        get_agent_session_runtime_marker=lambda *_args, **_kwargs: dict(marker) or None,
        set_agent_session_runtime_marker=persist,
    )
    agent.ensure_agent_session_id = Mock(return_value="contract-session")
    agent.bind_agent_session_id = Mock(return_value="contract-session")
    agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
    agent._caller_env_for_request = Mock(return_value={})
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


def _tool_names(tools):
    names = set()
    for tool in tools:
        if tool.get("type") == "namespace":
            names.update(_tool_names(tool.get("tools", [])))
        elif name := tool.get("name"):
            names.add(name)
    return names


@asynccontextmanager
async def _native_server(tmp_path, *, configured_instructions=None):
    requests = []
    completed = asyncio.Queue()
    harness = SimpleNamespace(requests=requests, next_input_tokens=10)

    async def responses(request):
        requests.append(await request.json())
        response_id = f"resp-{len(requests)}"
        compacting = any(item.get("type") == "compaction_trigger" for item in requests[-1]["input"])
        tokens = harness.next_input_tokens
        harness.next_input_tokens = 10
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
                "response": {
                    "id": response_id,
                    "usage": {"input_tokens": tokens, "output_tokens": 1, "total_tokens": tokens + 1},
                },
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
                        # Eligible for the independent async path: the host's
                        # synchronous opt-out must not depend on catalog edits.
                        "experimental_supported_tools": ["request_user_input_async"],
                        "truncation_policy": {"mode": "tokens", "limit": 10000},
                        "base_instructions": "You are a test assistant.",
                        "model_messages": {"collaboration_modes": {"default": CATALOG_PROMPT}},
                    }
                ]
            }
        )
    )
    developer_config = (
        f"developer_instructions = {json.dumps(configured_instructions, ensure_ascii=False)}\n"
        if configured_instructions else ""
    )
    (codex_home / "config.toml").write_text(
        f'model = "{MODEL}"\nmodel_provider = "contract"\n'
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
        "model_auto_compact_token_limit = 100000\n"
        f"{developer_config}"
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

    harness.native = transport()
    harness.native.on_notification(notification)

    async def restart():
        await harness.native.stop()
        harness.native = transport()
        harness.native.on_notification(notification)
        await harness.native.start()
        return harness.native

    harness.restart = restart
    harness.finish_turn = finish_turn
    try:
        await harness.native.start()
        yield harness
        for model_request in requests:
            names = _tool_names(model_request["tools"])
            assert "request_user_input" not in names
            if not any(item.get("type") == "compaction_trigger" for item in model_request["input"]):
                assert {"exec_command", "write_stdin"} <= names
    finally:
        await harness.native.stop()
        server.close()
        await server.wait_closed()
        await runner.cleanup()


def _request(tmp_path):
    return SimpleNamespace(
        session_key="contract",
        base_session_id="contract",
        working_path=str(tmp_path),
        composite_session_id="avibe:contract",
        subagent_name=None,
        subagent_model=None,
        subagent_reasoning_effort=None,
        context=SimpleNamespace(platform_specific={"agent_session_id": "contract-session"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [None, "collaboration", "fallback"])
async def test_native_model_receives_injected_prompt_once_across_turns_and_restart(tmp_path, legacy):
    async with _native_server(tmp_path) as harness:
        native, requests, finish_turn = harness.native, harness.requests, harness.finish_turn
        thread = await native.send_request("thread/start", {"cwd": str(tmp_path), "model": MODEL})
        thread_id = thread["thread"]["id"]
        marker = {}
        if legacy == "collaboration":
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
            marker.update(thread_id=thread_id, strategy="collaboration", sha256=hashlib.sha256(PROMPT.encode()).hexdigest())
        elif legacy == "fallback":
            await native.send_request("thread/inject_items", {
                "threadId": thread_id,
                "items": [
                    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}
                    for text in ("An obsolete Skill is enabled.", PROMPT)
                ],
            })
            marker.update(thread_id=thread_id, strategy="fallback", sha256=hashlib.sha256(PROMPT.encode()).hexdigest())

        request = _request(tmp_path)
        agent = _agent(marker)
        for _ in range(2):
            await agent._start_turn(native, request, thread_id, developer_instructions=PROMPT)
            await finish_turn()
            assert _developer_texts(requests[-1]).count(CodexAgent._render_developer_prompt_snapshot(PROMPT)) == 1
            assert requests[-1]["model"] == MODEL
            assert requests[-1]["reasoning"]["effort"] == "high"

        native = await harness.restart()
        await native.send_request("thread/resume", {"threadId": thread_id})
        agent = _agent(marker)
        await agent._start_turn(native, request, thread_id, developer_instructions=PROMPT)
        await finish_turn()
        assert _developer_texts(requests[-1]).count(CodexAgent._render_developer_prompt_snapshot(PROMPT)) == 1

        # Removing a prompt source must revoke its earlier positive rules,
        # including when retained history still contains the old snapshot.
        changed = (Path(__file__).resolve().parents[1] / "core/prompts/session-title.md").read_text()
        changed_snapshot = CodexAgent._render_developer_prompt_snapshot(changed)
        await agent._start_turn(native, request, thread_id, developer_instructions=changed)
        await finish_turn()
        assert _developer_texts(requests[-1]).count(changed_snapshot) == 1
        assert "## Quick-reply buttons" not in changed_snapshot
        assert "omitted from this snapshot no longer apply" in changed_snapshot
        assert "including untagged versions" in changed_snapshot

        await native.send_request("thread/compact/start", {"threadId": thread_id})
        await finish_turn()
        assert any(item.get("type") == "compaction_trigger" for item in requests[-1]["input"])
        await agent._start_turn(native, request, thread_id, developer_instructions=changed)
        await finish_turn()
        snapshots = [text for text in _developer_texts(requests[-1]) if text.startswith("<avibe_runtime_instructions>")]
        assert snapshots.count(changed_snapshot) == 1
        assert snapshots[-1] == changed_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_pressure", [False, True])
async def test_native_baseline_survives_auto_compaction_and_cold_promotion(tmp_path, retention_pressure):
    configured = "Native user preference: 保留我自己的规则。"
    baseline = "AVIBE_BASELINE_A：完整基线。\n" + PROMPT
    changed = "AVIBE_BASELINE_B：更新后基线。\n" + PROMPT
    snapshot = CodexAgent._render_developer_prompt_snapshot(changed)

    async with _native_server(tmp_path, configured_instructions=configured) as harness:
        native = harness.native
        request = _request(tmp_path)
        marker = {}
        agent = _agent(marker)
        thread_id = await agent._start_or_resume_thread(native, request, developer_instructions=baseline)

        async def turn(prompt):
            await agent._start_turn(native, request, thread_id, developer_instructions=prompt)
            await harness.finish_turn()
            return "\n".join(_developer_texts(harness.requests[-1]))

        for _ in range(2):
            text = await turn(baseline)
            assert text.count(configured) == 1
            assert text.count(baseline) == 1
            assert "<avibe_runtime_instructions>" not in text

        # Restart on unchanged bytes: use Avibe's actual resume path, not only
        # the native RPC, and verify that its durable marker avoids injection.
        native = await harness.restart()
        agent = _agent(marker)
        assert await agent._start_or_resume_thread(native, request, developer_instructions=baseline) == thread_id
        text = await turn(baseline)
        assert text.count(baseline) == 1
        assert "<avibe_runtime_instructions>" not in text

        # Inject the newer overlay before a recent user item. Its reported usage
        # forces automatic pre-turn compaction on the next actual turn/start.
        if retention_pressure:
            agent._build_input.return_value = [
                {"type": "text", "text": "Recent history data. " * 18000, "text_elements": []}
            ]
        harness.next_input_tokens = 200000
        text = await turn(changed)
        assert text.count(baseline) == 1
        assert text.count(snapshot) == 1
        agent._build_input.return_value = [{"type": "text", "text": "Continue", "text_elements": []}]
        prior_requests = len(harness.requests)
        text = await turn(changed)
        assert any(
            item.get("type") == "compaction_trigger"
            for body in harness.requests[prior_requests:]
            for item in body["input"]
        )
        assert text.count(configured) == 1
        assert text.count(baseline) == 1
        # A native baseline survives budget pressure. An injected overlay does
        # not: characterize this limit rather than claim latest-version safety.
        assert text.count(snapshot) == int(not retention_pressure)

        native = await harness.restart()
        agent = _agent(marker)
        assert await agent._start_or_resume_thread(native, request, developer_instructions=changed) == thread_id
        text = await turn(changed)
        assert text.count(baseline) == 1  # Cold configuration does not rewrite restored history.
        assert text.count(snapshot) == int(not retention_pressure)

        await native.send_request("thread/compact/start", {"threadId": thread_id})
        await harness.finish_turn()
        text = await turn(changed)
        assert baseline not in text
        assert text.count(configured) == 1
        assert text.count(changed) == 1 + int(not retention_pressure)
        assert text.count(snapshot) == int(not retention_pressure)


@pytest.mark.asyncio
async def test_native_fork_receives_target_prompt_before_and_after_compaction(tmp_path):
    source_prompt = "SOURCE_AGENT：原会话。"
    target_prompt = "TARGET_AGENT：新会话。"
    configured = "Independently configured native preference."
    async with _native_server(tmp_path, configured_instructions=configured) as harness:
        native = harness.native
        request = _request(tmp_path)
        marker = {}
        source = _agent(marker)
        source_id = await source._start_or_resume_thread(native, request, developer_instructions=source_prompt)
        await source._start_turn(native, request, source_id, developer_instructions=source_prompt)
        await harness.finish_turn()

        target_marker = dict(marker)
        target = _agent(target_marker)
        target_id = await target._fork_thread(
            native,
            request,
            {"source_session_id": "contract-session", "source_native_session_id": source_id},
            developer_instructions=target_prompt,
        )
        assert target_id != source_id
        assert target_marker["sha256"] == source._prompt_fingerprint(source_prompt)
        await target._start_turn(native, request, target_id, developer_instructions=target_prompt)
        await harness.finish_turn()
        text = "\n".join(_developer_texts(harness.requests[-1]))
        assert text.count(source_prompt) == 1
        assert text.count(CodexAgent._render_developer_prompt_snapshot(target_prompt)) == 1

        await native.send_request("thread/compact/start", {"threadId": target_id})
        await harness.finish_turn()
        await target._start_turn(native, request, target_id, developer_instructions=target_prompt)
        await harness.finish_turn()
        text = "\n".join(_developer_texts(harness.requests[-1]))
        assert source_prompt not in text
        assert text.count(configured) == 1
        assert text.count(target_prompt) == 2  # Native baseline plus retained overlay.
