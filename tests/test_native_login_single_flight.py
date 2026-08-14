from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent_auth_service import (
    OPENCODE_DIRECT_SETUP_URLS,
    AgentAuthFlow,
    AgentAuthService,
    BackendLoginInProgressError,
    WebAuthFlow,
)
from core.handlers.model_hub.native_oauth import AgentAuthNativeOAuthAdapter
from modules.im import MessageContext


class _IMClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, _context, text: str) -> None:
        self.sent.append(text)


class _Controller:
    def __init__(self) -> None:
        self.im_client = _IMClient()
        self.config = SimpleNamespace(language="en")

    def resolve_agent_for_context(self, _context) -> str:
        return "codex"

    def _get_settings_key(self, context) -> str:
        return context.channel_id or "channel"

    def _get_lang(self) -> str:
        return "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(AgentAuthService.WEB_BACKENDS))
async def test_every_web_backend_publishes_deadline_after_startup_with_waiter(
    backend: str,
) -> None:
    """Drive the backend catalog so every usable Web flow shares the invariant."""
    service = AgentAuthService(_Controller())
    startup_finished: datetime | None = None

    async def start_codex(**_kwargs):
        nonlocal startup_finished
        startup_finished = datetime.now(timezone.utc)
        return SimpleNamespace(stdout=None, returncode=None)

    async def start_claude(_context, **_kwargs):
        nonlocal startup_finished
        startup_finished = datetime.now(timezone.utc)
        return object(), "https://claude.ai/oauth/authorize", None

    async def start_provider_oauth(*_args, **_kwargs):
        nonlocal startup_finished
        startup_finished = datetime.now(timezone.utc)
        return {"url": "https://provider.example/oauth"}

    server = SimpleNamespace(
        get_provider_auth=AsyncMock(return_value={}),
        start_provider_oauth=start_provider_oauth,
    )
    service._start_codex_process = start_codex
    service._start_claude_control_flow = start_claude
    service._opencode_server = AsyncMock(return_value=server)
    service._wait_for_codex_completion_web = AsyncMock()
    service._wait_for_claude_completion_web = AsyncMock()
    service._wait_for_opencode_oauth_web = AsyncMock()

    flow = await service.start_web_setup(
        backend,
        force_reset=False,
        provider_id="opencode" if backend == "opencode" else None,
    )

    assert isinstance(flow, WebAuthFlow)
    assert startup_finished is not None
    assert flow.waiter_task is not None
    assert flow.expires_at_iso is not None
    assert datetime.fromisoformat(flow.expires_at_iso) >= startup_finished + timedelta(
        seconds=service.setup_timeout_seconds
    )
    await flow.waiter_task


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(AgentAuthService.WEB_BACKENDS))
async def test_every_im_native_backend_publishes_deadline_after_startup_with_waiter(
    backend: str,
) -> None:
    """Drive the backend catalog through the shared IM flow contract."""
    service = AgentAuthService(_Controller())
    startup_finished: datetime | None = None
    process = SimpleNamespace(stdout=None, returncode=None)

    async def start_codex(**_kwargs):
        nonlocal startup_finished
        startup_finished = datetime.now(timezone.utc)
        return process

    async def start_claude(_context, **_kwargs):
        nonlocal startup_finished
        startup_finished = datetime.now(timezone.utc)
        return object(), "https://claude.ai/oauth/authorize", None

    async def start_opencode(_context, **_kwargs):
        nonlocal startup_finished
        startup_finished = datetime.now(timezone.utc)
        return process, 1, "oauth-provider"

    service._resolve_opencode_provider = AsyncMock(return_value="oauth-provider")
    service._start_codex_process = start_codex
    service._start_claude_control_flow = start_claude
    service._start_opencode_process = start_opencode
    service._read_codex_output = AsyncMock()
    service._read_pty_output = AsyncMock()
    service._wait_for_completion = AsyncMock()
    service._wait_for_claude_completion = AsyncMock()

    flow = await service._start_auth_flow(
        backend,
        context=MessageContext(user_id="user", channel_id=backend),
        force_reset=False,
        claude_login_method="claudeai",
    )

    assert isinstance(flow, AgentAuthFlow)
    assert startup_finished is not None
    assert flow.waiter_task is not None
    assert flow.expires_at_iso is not None
    assert datetime.fromisoformat(flow.expires_at_iso) >= startup_finished + timedelta(
        seconds=service.setup_timeout_seconds
    )
    await flow.waiter_task


@pytest.mark.asyncio
async def test_failed_and_waiterless_flows_do_not_advertise_deadlines() -> None:
    service = AgentAuthService(_Controller())
    service._start_codex_process = AsyncMock(side_effect=RuntimeError("spawn failed"))

    failed = await service.start_web_setup("codex", force_reset=False)

    assert failed.state == "failed"
    assert failed.waiter_task is None
    assert failed.expires_at_iso is None

    provider = next(iter(OPENCODE_DIRECT_SETUP_URLS))
    service._resolve_opencode_provider = AsyncMock(return_value=provider)
    direct = await service._start_auth_flow(
        "opencode",
        context=MessageContext(user_id="user", channel_id="direct"),
        force_reset=False,
    )

    assert isinstance(direct, AgentAuthFlow)
    assert direct.waiter_task is None
    assert direct.expires_at_iso is None


@pytest.mark.asyncio
async def test_single_service_fixture_routes_all_callers_to_the_same_registry() -> None:
    """Prove in-instance reuse, not cross-process production exclusion.

    Production intentionally constructs separate AgentAuthService instances in
    the controller and UI-server processes.
    """
    controller = _Controller()
    service = AgentAuthService(controller)
    process = SimpleNamespace(stdout=object(), returncode=0)
    service._start_codex_process = AsyncMock(return_value=process)
    service._read_codex_output = AsyncMock()
    service._wait_for_completion = AsyncMock()

    context = MessageContext(user_id="user", channel_id="channel")
    await service.start_setup(context, backend="codex", force_reset=False)
    im_flow = service._flows["channel:codex"]

    assert service._web_flows is service._flows_by_id
    assert service._web_flows[im_flow.flow_id] is im_flow

    with pytest.raises(BackendLoginInProgressError):
        await service.start_web_setup("codex", force_reset=False, owner_ref="source")

    service._drop_flow(im_flow)
    web_flow = await service.start_web_setup("codex", force_reset=False, owner_ref="source")
    assert service._flows_by_id[web_flow.flow_id] is web_flow

    adapter = AgentAuthNativeOAuthAdapter(
        service,
        auth_status_reader=lambda _backend: {"active_auth_mode": "oauth"},
    )
    assert (await adapter.oauth_status(web_flow.flow_id)).flow_id == web_flow.flow_id
    await adapter.cancel_oauth(web_flow.flow_id)


@pytest.mark.asyncio
async def test_model_hub_drives_the_shared_web_flow_through_submit_and_cancel() -> None:
    """Model Hub delegates one Web flow lifecycle within a service instance."""
    service = AgentAuthService(_Controller())
    client = object()
    service._start_claude_control_flow = AsyncMock(
        return_value=(client, "https://claude.ai/oauth/authorize", None)
    )
    service._wait_for_claude_completion_web = AsyncMock()
    service._send_claude_callback = AsyncMock()
    service._terminate_web_flow = AsyncMock()
    adapter = AgentAuthNativeOAuthAdapter(
        service,
        auth_status_reader=lambda _backend: {"active_auth_mode": "oauth"},
    )

    state = await adapter.start_oauth("source", "anthropic")
    flow = service._flows_by_id[state.flow_id]
    assert (await adapter.oauth_status(state.flow_id)).flow_id == flow.flow_id

    submitted = await adapter.submit_oauth(state.flow_id, "authorization#state")
    assert submitted.state == "verifying"
    service._send_claude_callback.assert_awaited_once_with(
        client, "authorization", "state"
    )
    assert service._flows_by_id[state.flow_id] is flow

    await adapter.cancel_oauth(state.flow_id)
    service._terminate_web_flow.assert_awaited_once_with(
        flow, final_state="cancelled"
    )
    assert state.flow_id not in service._flows_by_id


@pytest.mark.asyncio
async def test_cancel_keeps_native_claim_until_termination_settles() -> None:
    service = AgentAuthService(_Controller())
    process = SimpleNamespace(stdout=None, returncode=None)
    service._start_codex_process = AsyncMock(return_value=process)
    service._read_codex_output_web = AsyncMock()
    service._wait_for_codex_completion_web = AsyncMock()
    flow = await service.start_web_setup("codex", force_reset=False)
    termination_started = asyncio.Event()
    finish_termination = asyncio.Event()

    async def terminate(_flow, **_kwargs) -> None:
        termination_started.set()
        await finish_termination.wait()

    service._terminate_web_flow = terminate
    cancel_task = asyncio.create_task(service.cancel_web_flow(flow.flow_id))
    await termination_started.wait()

    try:
        with pytest.raises(BackendLoginInProgressError):
            await service.start_web_setup("codex", force_reset=False)
    finally:
        finish_termination.set()
        assert await cancel_task == {"ok": True}

    replacement = await service.start_web_setup("codex", force_reset=False)
    assert replacement.flow_id in service._flows_by_id


@pytest.mark.asyncio
async def test_cancel_releases_native_claim_when_termination_raises() -> None:
    service = AgentAuthService(_Controller())
    process = SimpleNamespace(stdout=None, returncode=None)
    service._start_codex_process = AsyncMock(return_value=process)
    service._read_codex_output_web = AsyncMock()
    service._wait_for_codex_completion_web = AsyncMock()
    flow = await service.start_web_setup("codex", force_reset=False)
    service._terminate_web_flow = AsyncMock(side_effect=RuntimeError("terminate failed"))

    with pytest.raises(RuntimeError, match="terminate failed"):
        await service.cancel_web_flow(flow.flow_id)

    assert flow.flow_id not in service._flows_by_id
    replacement = await service.start_web_setup("codex", force_reset=False)
    assert replacement.flow_id in service._flows_by_id
