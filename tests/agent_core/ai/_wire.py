"""The request an adapter sends, built without sending it: the transform, then the protocol's ``wire_request``."""

from __future__ import annotations

from typing import Optional

from core.agent_core.ai import anthropic, google, openai_chat, openai_responses
from core.agent_core.ai._common import WireRequest, endpoint_origin, prepare_messages
from core.agent_core.ai.provider import MediaLoader, ModelRequest, ProviderError
from core.agent_core.cancel import CancelToken

_WIRE_REQUESTS = {
    "anthropic": anthropic.wire_request,
    "google": google.wire_request,
    "openai_chat": openai_chat.wire_request,
    "openai_responses": openai_responses.wire_request,
}


async def build_wire_request(
    request: ModelRequest,
    *,
    gateway: bool,
    media_loader: Optional[MediaLoader] = None,
) -> WireRequest:
    """Exactly what ``request.endpoint.protocol``'s adapter sends for ``request``. Raises ``ValueError`` when the
    request cannot be built."""

    protocol = request.endpoint.protocol
    prepared = await prepare_messages(
        request.messages,
        target=endpoint_origin(request.endpoint),
        supports_images=request.supports_images,
        protocol=protocol,
        media_loader=media_loader,
        cancel=CancelToken(),
        endpoint=request.endpoint,
    )
    if isinstance(prepared, ProviderError):
        raise ValueError(prepared.message)
    messages, loaded_images = prepared
    return _WIRE_REQUESTS[protocol](request, messages, gateway=gateway, loaded_images=loaded_images)
