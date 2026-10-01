"""A healthy OpenCode server can still be bootstrapping the turn's directory.

OpenCode answers ``/global/health`` as soon as it serves, but bootstraps a
per-directory instance on that directory's first request. On a fresh host the
bootstrap waits for OpenCode's own npm install and outlasts Avibe's ordinary
request timeout, so a first turn or provider probe must wait for it instead of failing.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web

import modules.agents.opencode.server as server_module
from config.v2_config import V2Config
from core.agent_auth_service import AgentAuthService
from modules.agents.base import AgentRequest
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.opencode.server import OpenCodeServerManager
from modules.agents.opencode.session import OpenCodeSessionManager
from modules.im import MessageContext

REQUEST_TIMEOUT_SECONDS = 1


@asynccontextmanager
async def _cold_opencode(bootstrap_seconds: float):
    """Serve OpenCode's cold-start contract over real HTTP.

    Health answers at once. Every directory-scoped request waits for that
    directory's one-time bootstrap, which keeps running when a client gives up.
    """

    bootstraps: dict[str, asyncio.Task] = {}
    opencode = SimpleNamespace(port=0, created_sessions=[], prompts=[])

    async def bootstrapped(request: web.Request) -> None:
        directory = request.headers["x-opencode-directory"]
        if directory not in bootstraps:
            bootstraps[directory] = asyncio.ensure_future(asyncio.sleep(bootstrap_seconds))
        await asyncio.shield(bootstraps[directory])

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"healthy": True, "version": "1.18.18"})

    async def path(request: web.Request) -> web.Response:
        await bootstrapped(request)
        return web.json_response({"directory": request.headers["x-opencode-directory"]})

    async def providers(request: web.Request) -> web.Response:
        await bootstrapped(request)
        return web.json_response({"providers": [{"id": "openai", "models": {"gpt-cold": {}}}], "default": {}})

    async def create_session(request: web.Request) -> web.Response:
        await bootstrapped(request)
        session_id = f"ses_cold_{len(opencode.created_sessions) + 1}"
        opencode.created_sessions.append(session_id)
        return web.json_response({"id": session_id})

    async def prompt_async(request: web.Request) -> web.Response:
        await bootstrapped(request)
        opencode.prompts.append(await request.json())
        return web.Response(status=204)

    async def messages(request: web.Request) -> web.Response:
        await bootstrapped(request)
        if not opencode.prompts:
            return web.json_response([])
        reply = {
            "info": {"id": "msg_reply", "role": "assistant", "time": {"completed": 1}, "finish": "stop"},
            "parts": [{"type": "text", "text": "OK"}],
        }
        return web.json_response([reply])

    async def abort_session(request: web.Request) -> web.Response:
        await bootstrapped(request)
        return web.json_response(True)

    app = web.Application()
    app.router.add_get("/global/health", health)
    app.router.add_get("/path", path)
    app.router.add_get("/config/providers", providers)
    app.router.add_post("/session", create_session)
    app.router.add_post("/session/{session_id}/prompt_async", prompt_async)
    app.router.add_get("/session/{session_id}/message", messages)
    app.router.add_post("/session/{session_id}/abort", abort_session)
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        opencode.port = runner.addresses[0][1]
        yield opencode
    finally:
        for task in bootstraps.values():
            task.cancel()
        await runner.cleanup()


async def _first_turn(tmp_path, port: int, monkeypatch):
    """Run a fresh thread's first turn up to the point it would prompt."""

    reached_prompt: list[tuple[str, str, str] | None] = []
    failures: list[str] = []

    async def emit_backend_failure(_controller, _context, _backend, _error, *, display_text, request, **_kwargs):
        failures.append(display_text)

    monkeypatch.setattr("modules.agents.opencode.agent.emit_backend_failure", emit_backend_failure)

    server = OpenCodeServerManager(port=port, request_timeout_seconds=REQUEST_TIMEOUT_SECONDS)
    # The process is up and healthy; its lifecycle is not what this covers.
    server.ensure_running = AsyncMock(return_value=server.base_url)

    agent = object.__new__(OpenCodeAgent)
    sessions = SimpleNamespace(
        get_agent_session_id=Mock(return_value=None),
        ensure_agent_session_id=Mock(return_value="sesavibe01"),
        bind_agent_session=Mock(return_value="sesavibe01"),
        remove_active_poll=Mock(),
    )
    agent.sessions = sessions
    agent._session_manager = OpenCodeSessionManager(SimpleNamespace(sessions=sessions), "opencode")

    def stop_before_prompt(_context):
        reached_prompt.append(agent._session_manager.get_request_session(request.base_session_id))
        raise RuntimeError("test boundary before the prompt")

    agent.controller = SimpleNamespace(
        config=SimpleNamespace(language="en", platform="slack"),
        get_opencode_overrides=stop_before_prompt,
    )
    agent.config = agent.controller.config
    agent._get_server = AsyncMock(return_value=server)
    agent._delete_ack = AsyncMock()
    agent._remove_ack_reaction = AsyncMock()
    agent.record_model_hub_native_failure = AsyncMock()
    agent._steering_states = {}

    working_path = str(tmp_path / "workspace")
    request = AgentRequest(
        context=MessageContext(user_id="U1", channel_id="C1", platform="slack", platform_specific={}),
        message="hello",
        user_message="hello",
        working_path=working_path,
        base_session_id="base-1",
        composite_session_id=f"base-1:{working_path}",
        session_key="slack::channel::C1",
    )
    try:
        await agent._process_message(request)
    finally:
        await server.close_http_session()
    return working_path, reached_prompt, failures


def test_first_turn_waits_for_directory_bootstrap_longer_than_request_timeout(tmp_path, monkeypatch) -> None:
    async def scenario():
        async with _cold_opencode(bootstrap_seconds=REQUEST_TIMEOUT_SECONDS + 1) as opencode:
            working_path, reached_prompt, failures = await _first_turn(tmp_path, opencode.port, monkeypatch)
        return working_path, reached_prompt, failures, opencode.created_sessions

    working_path, reached_prompt, failures, created_sessions = asyncio.run(scenario())

    assert reached_prompt == [("ses_cold_1", working_path, "slack::channel::C1")]
    assert created_sessions == ["ses_cold_1"]
    assert failures == ["OpenCode request failed: RuntimeError: test boundary before the prompt"]


def test_bootstrap_past_the_readiness_ceiling_reports_first_time_setup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "DIRECTORY_BOOTSTRAP_TIMEOUT", REQUEST_TIMEOUT_SECONDS, raising=False)

    async def scenario():
        async with _cold_opencode(bootstrap_seconds=REQUEST_TIMEOUT_SECONDS + 2) as opencode:
            _working_path, reached_prompt, failures = await _first_turn(tmp_path, opencode.port, monkeypatch)
        return reached_prompt, failures, opencode.created_sessions

    reached_prompt, failures, created_sessions = asyncio.run(scenario())

    assert reached_prompt == []
    assert created_sessions == []
    assert failures == ["❌ OpenCode is still finishing its first-time setup. Send your message again shortly."]


def test_provider_probe_lists_models_after_directory_bootstrap_longer_than_request_timeout(monkeypatch) -> None:
    # The probe tests native connectivity, which only direct mode allows.
    config = V2Config.default()
    config.model_hub.agents["opencode"].mode = "direct"
    config.save()

    async def scenario():
        async with _cold_opencode(bootstrap_seconds=REQUEST_TIMEOUT_SECONDS + 1) as opencode:
            server = OpenCodeServerManager(port=opencode.port, request_timeout_seconds=REQUEST_TIMEOUT_SECONDS)
            service = AgentAuthService(SimpleNamespace(config=SimpleNamespace(language="en")))
            monkeypatch.setattr(service, "_opencode_server", AsyncMock(return_value=server))
            try:
                result = await service.test_opencode_provider("openai")
            finally:
                await server.close_http_session()
        return result, opencode.prompts

    result, prompts = asyncio.run(scenario())

    assert result["ok"] is True, result
    assert result["model"] == "gpt-cold"
    assert [prompt["model"] for prompt in prompts] == [{"providerID": "openai", "modelID": "gpt-cold"}]
