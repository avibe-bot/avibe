"""Native protocol adapter registry."""

from __future__ import annotations

from typing import Any, Callable, Optional

from core.agent_core.ai import anthropic, openai_chat, openai_responses
from core.agent_core.ai._common import WireRequest, endpoint_origin, prepare_messages
from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.openai_chat import OpenAIChatAdapter
from core.agent_core.ai.openai_responses import OpenAIResponsesAdapter
from core.agent_core.ai.provider import MediaLoader, ModelRequest, ProviderAdapter, ProviderError
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import ProtocolName

ADAPTERS: dict[ProtocolName, type[ProviderAdapter]] = {
    "anthropic": AnthropicAdapter,
    "openai_chat": OpenAIChatAdapter,
    "openai_responses": OpenAIResponsesAdapter,
}

_WIRE_REQUESTS: dict[ProtocolName, Callable[..., WireRequest]] = {
    "anthropic": anthropic.wire_request,
    "openai_chat": openai_chat.wire_request,
    "openai_responses": openai_responses.wire_request,
}


def adapter_class(protocol: ProtocolName) -> type[ProviderAdapter]:
    try:
        return ADAPTERS[protocol]
    except KeyError as exc:
        raise ValueError(f"unsupported provider protocol: {protocol}") from exc


def create_adapter(protocol: ProtocolName, **kwargs: Any) -> ProviderAdapter:
    """Construct the adapter registered for ``protocol``."""

    return adapter_class(protocol)(**kwargs)  # type: ignore[call-arg]


get_adapter = create_adapter


async def build_wire_request(
    request: ModelRequest,
    *,
    gateway: bool,
    media_loader: Optional[MediaLoader] = None,
    cancel: Optional[CancelToken] = None,
) -> WireRequest:
    """The request ``request.endpoint.protocol``'s adapter would send for ``request``, without sending it.

    The adapter's ``stream`` sends exactly this, so a hosted-tool probe built here is the production request
    (``vibey-hosted-tools.md`` §3.2). Raises ``ValueError`` when the request cannot be built.
    """

    protocol = request.endpoint.protocol
    if protocol not in _WIRE_REQUESTS:
        raise ValueError(f"unsupported provider protocol: {protocol}")
    prepared = await prepare_messages(
        request.messages,
        target=endpoint_origin(request.endpoint),
        supports_images=request.supports_images,
        protocol=protocol,
        media_loader=media_loader,
        cancel=cancel or CancelToken(),
    )
    if isinstance(prepared, ProviderError):
        raise ValueError(prepared.message)
    messages, loaded_images = prepared
    return _WIRE_REQUESTS[protocol](request, messages, gateway=gateway, loaded_images=loaded_images)
