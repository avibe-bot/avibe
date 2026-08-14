from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.agent_auth_service import AgentAuthService, BackendLoginInProgressError
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
async def test_im_web_and_model_hub_use_the_same_flow_registry() -> None:
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
